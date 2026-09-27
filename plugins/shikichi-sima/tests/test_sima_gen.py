# -*- coding: utf-8 -*-
# 提供: 有限会社福井工務店（2026）
"""test_sima_gen — 測量座標から SIMA／DXF を作る道具

確かめること。
1 書式が実機合格版と1バイトも違わない。読み戻すと桁位置・CP932・CRLF・G00〜G99 の並びがそろう
2 座標を 0.5m 写し間違えると、辺長と面積の検算で止まり、.sim も .dxf も作らない
3 DXF を読み戻すと、地番ごとに閉じた線・頂点数・軸の向き(x=東 y=北)・mm 換算・原点移動が合う
4 全角の地番名・点名でも桁がズレない。20バイトを超える名前は止まる
5 出力はそろって書くか1つも書かないか。既にある物は黙って上書きしない

**座標はすべて架空の土地。** 実在の案件の座標は使わない。
"""

import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(BIN))
import sima_gen  # noqa: E402

TOOL = BIN / "sima_gen.py"

# 架空の分譲地。宅地1区画と前面道路。K1・K4 は2つの地番で共有する点。
# 距離は次の点まで(m・小数2桁)、地積は m2(小数2桁)。図面の書き方に合わせて丸めてある。
FIXTURE = {
    "genba": "架空の分譲地 1号地（試験用）",
    "chiban": [
        {"name": "1号地", "area": 186.57, "points": [
            ["K1", -148203.417, -31856.284, 13.98],
            ["K2", -148191.062, -31849.735, 13.33],
            ["Ｋ３", -148184.905, -31861.558, 14.04],
            ["K4", -148197.321, -31868.109, 13.3],
        ]},
        {"name": "道路1", "area": 67.32, "points": [
            ["K1", -148203.417, -31856.284, 13.3],
            ["K4", -148197.321, -31868.109, 5.31],
            ["R1", -148201.093, -31871.846, 13.35],
            ["R2", -148207.188, -31859.972, 5.27],
        ]},
    ],
}

# 上の架空の土地を、実機で合格した元の道具に通して出た .sim。書式の回帰を1バイト単位で固定する。
GOLDEN = [
    "G00,01,架空の分譲地 1号地（試験用）,",
    "A00,",
    "A01,   1,K1                  ,  -148203.41700000,   -31856.28400000,    0.00000000,",
    "A01,   2,K2                  ,  -148191.06200000,   -31849.73500000,    0.00000000,",
    "A01,   3,Ｋ３                ,  -148184.90500000,   -31861.55800000,    0.00000000,",
    "A01,   4,K4                  ,  -148197.32100000,   -31868.10900000,    0.00000000,",
    "A01,   5,R1                  ,  -148201.09300000,   -31871.84600000,    0.00000000,",
    "A01,   6,R2                  ,  -148207.18800000,   -31859.97200000,    0.00000000,",
    "A99,",
    "D00,   1,1号地               ,1,",
    "B01,   1,K1                  ,",
    "B01,   2,K2                  ,",
    "B01,   3,Ｋ３                ,",
    "B01,   4,K4                  ,",
    "D99,",
    "D00,   2,道路1               ,1,",
    "B01,   1,K1                  ,",
    "B01,   4,K4                  ,",
    "B01,   5,R1                  ,",
    "B01,   6,R2                  ,",
    "D99,",
    "G99,",
]

# 全角ちょうど10文字 = CP932 で20バイト。桁いっぱいの名前。
FULL_20 = "境界点ＡＢＣＤＥＦＧ"
LOT_20 = "全角地番名ちょうど十"


def fixture():
    return copy.deepcopy(FIXTURE)


def data():
    return sima_gen.normalize(fixture())


def quiet(func, *args):
    """検算の画面出力を試験の出力に混ぜない。"""
    with mock.patch("sys.stdout", new=open(os.devnull, "w", encoding="utf-8")) as out:
        try:
            return func(*args)
        finally:
            out.close()


def run_tool(*args):
    proc = subprocess.run([sys.executable, str(TOOL)] + [str(a) for a in args],
                          capture_output=True, timeout=60)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def sim_bytes(d):
    text, _ = sima_gen.build(d)
    return text.encode("cp932")


class Sima(unittest.TestCase):
    def test_書式が実機合格版と1バイトも違わない(self):
        expect = ("\r\n".join(GOLDEN) + "\r\n").encode("cp932")
        self.assertEqual(sim_bytes(data()), expect)

    def test_改行はCRLFだけで文字はCP932(self):
        raw = sim_bytes(data())
        self.assertTrue(raw.endswith(b"G99,\r\n"))
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertEqual(raw.count(b"\r"), raw.count(b"\r\n"))
        raw.decode("cp932")
        self.assertEqual(sima_gen.readback_sim(raw, data()), [])

    def test_読み戻しで座標や点の並びの取り違えを見つける(self):
        raw = sim_bytes(data())
        moved = raw.replace(b"-148191.06200000", b"-148191.56200000")   # K2 の X を 0.5m
        self.assertTrue(any("A01" in p for p in sima_gen.readback_sim(moved, data())))
        swapped = raw.replace(b"B01,   2,K2", b"B01,   9,K9").replace(b"B01,   3,", b"B01,   2,")
        swapped = swapped.replace(b"B01,   9,K9", b"B01,   3,K2")
        self.assertTrue(any("D00/B01" in p for p in sima_gen.readback_sim(swapped, data())))

    def test_G00からG99まで決まった並びになる(self):
        heads = "".join(line[:3] + " " for line in
                        sim_bytes(data()).decode("cp932").split("\r\n")[:-1])
        shape = r"^G00 A00 (A01 )+A99 (D00 (B01 )+D99 )+G99 $"
        self.assertRegex(heads, shape)

    def test_A01を桁位置で読み戻すと座標が元の値に戻る(self):
        d = data()
        allpts, _ = sima_gen.unique_points(d)
        lines = [ln.encode("cp932") for ln in
                 sim_bytes(d).decode("cp932").split("\r\n") if ln.startswith("A01,")]
        self.assertEqual(len(lines), len(allpts))
        for b, (nm, x, y, z) in zip(lines, allpts):
            # A01,(4桁),(点名20バイト),(X 18桁),(Y 18桁),(標高 14桁),
            self.assertEqual([b[3:4], b[8:9], b[29:30], b[48:49], b[67:68], b[82:83]], [b","] * 6)
            self.assertEqual(len(b), 83)
            self.assertEqual(b[9:29].decode("cp932").rstrip(" "), nm)
            self.assertAlmostEqual(float(b[30:48]), x, places=8)
            self.assertAlmostEqual(float(b[49:67]), y, places=8)
            self.assertAlmostEqual(float(b[68:82]), z, places=8)

    def test_共有点はA01に1回だけ出てB01は同じ番号を指す(self):
        text = sim_bytes(data()).decode("cp932")
        self.assertEqual(text.count("A01,"), 6)
        self.assertEqual(text.count("B01,   1,K1"), 2)
        self.assertEqual(text.count("B01,   4,K4"), 2)


class BigCoordinates(unittest.TestCase):
    def test_桁の大きい座標でも面積と重心が崩れない(self):
        # 上限近くの座標に置いた 10m x 7.5m の長方形。掛け算の桁落ちがあると面積がずれる
        x0, y0 = 9876543.21, -9123456.78
        pts = [("A", x0, y0), ("B", x0 + 10, y0), ("C", x0 + 10, y0 + 7.5), ("D", x0, y0 + 7.5)]
        pts = [(nm, x, y, None, 0.0) for nm, x, y in pts]
        self.assertAlmostEqual(sima_gen.shoelace(pts), 75.0, places=6)
        cx, cy = sima_gen.centroid(pts)
        self.assertAlmostEqual(cx, x0 + 5, places=6)
        self.assertAlmostEqual(cy, y0 + 3.75, places=6)


class ZenkakuNames(unittest.TestCase):
    def triangle(self, lot, point):
        return {"genba": "架空の三角地", "chiban": [{"name": lot, "points": [
            [point, 1000.25, 2000.5], ["P2", 1010.75, 2000.5], ["P3", 1000.25, 2012.125]]}]}

    def test_全角ちょうど20バイトの地番名と点名で桁がズレない(self):
        d = sima_gen.normalize(self.triangle(LOT_20, FULL_20))
        raw = sim_bytes(d)
        self.assertEqual(sima_gen.readback_sim(raw, d), [])
        for line in raw.split(b"\r\n"):
            if line[:4] in (b"A01,", b"B01,", b"D00,"):
                self.assertEqual(line[29:30], b",", line.decode("cp932"))
        self.assertIn(("D00,   1,%s,1," % LOT_20).encode("cp932"), raw)
        self.assertIn(("B01,   1,%s," % FULL_20).encode("cp932"), raw)

    def test_全角と半角が混ざっても桁がズレない(self):
        raw = sim_bytes(sima_gen.normalize(self.triangle("宅地A-1", "境界1号")))
        for line in raw.split(b"\r\n"):
            if line[:4] in (b"A01,", b"B01,", b"D00,"):
                self.assertEqual(line[29:30], b",")

    def test_21バイトの名前は止まる(self):
        with self.assertRaises(sima_gen.InputError):
            sima_gen.normalize(self.triangle(LOT_20, FULL_20 + "1"))
        with self.assertRaises(sima_gen.InputError):
            sima_gen.normalize(self.triangle(LOT_20 + "1", "P1"))


class Kenzan(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    def write_input(self, d, name="入力.json"):
        path = self.dir / name
        path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return path

    def test_正しい座標は検算に合格する(self):
        for cb in data()["chiban"]:
            _, ok = quiet(sima_gen.verify, cb["name"], cb["points"], cb["area"])
            self.assertTrue(ok, cb["name"])

    def test_正しい座標ならsimとdxfを書いて0で終わる(self):
        src = self.write_input(fixture())
        code, out = run_tool(src, "--dxf")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.dir / "入力.sim").is_file())
        self.assertTrue((self.dir / "入力.dxf").is_file())
        self.assertIn("[OK] 読み戻し検査 合格", out)

    def test_座標を0点5m写し間違えると止まり何も書かない(self):
        wrong = fixture()
        wrong["chiban"][0]["points"][1][1] += 0.5   # K2 の X を 0.5m ずらす
        src = self.write_input(wrong)
        code, out = run_tool(src, "--dxf")
        self.assertEqual(code, 1, out)
        self.assertIn("[STOP]", out)
        self.assertGreaterEqual(out.count("[NG] 辺長"), 2)
        self.assertIn("[NG] 面積", out)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["入力.json"])

    def test_検算に落ちた時は前の出力を今回の物と取り違えないよう知らせる(self):
        src = self.write_input(fixture())
        self.assertEqual(run_tool(src)[0], 0)
        before = (self.dir / "入力.sim").read_bytes()
        wrong = fixture()
        wrong["chiban"][1]["points"][2][2] -= 0.5   # R1 の Y を 0.5m ずらす
        self.write_input(wrong)
        code, out = run_tool(src, "--overwrite")
        self.assertEqual(code, 1)
        self.assertIn("前に作った物のまま", out)
        self.assertEqual((self.dir / "入力.sim").read_bytes(), before)


class Dxf(unittest.TestCase):
    def polylines(self, origin=None):
        raw = sima_gen.build_dxf(data(), origin).encode("cp932")
        return raw, sima_gen.dxf_polylines(sima_gen.dxf_pairs(raw))

    def test_読み戻すと区切りとEOFと画層がそろう(self):
        raw, _ = self.polylines()
        self.assertEqual(sima_gen.readback_dxf(raw, data()), [])
        pairs = sima_gen.dxf_pairs(raw)
        self.assertEqual(pairs[:2], [(0, "SECTION"), (2, "HEADER")])
        self.assertEqual(pairs[-1], (0, "EOF"))
        self.assertIn((1, "AC1009"), pairs)
        for layer in ("SHIKICHI", "TENMEI", "CHIBAN"):
            self.assertIn((2, layer), pairs)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))

    def test_地番ごとに閉じた線になり頂点数が合う(self):
        _, polys = self.polylines()
        self.assertEqual(len(polys), 2)
        for (closed, verts), cb in zip(polys, FIXTURE["chiban"]):
            self.assertTrue(closed)
            self.assertEqual(len(verts), len(cb["points"]))

    def test_xが東でyが北でmmになる(self):
        _, polys = self.polylines()
        k1 = FIXTURE["chiban"][0]["points"][0]
        vx, vy = polys[0][1][0]
        self.assertAlmostEqual(vx, k1[2] * 1000, places=3)   # x = Y(東)
        self.assertAlmostEqual(vy, k1[1] * 1000, places=3)   # y = X(北)

    def test_北にある点ほどyが大きく東にある点ほどxが大きい(self):
        _, polys = self.polylines()
        verts = polys[0][1]
        pts = FIXTURE["chiban"][0]["points"]
        north = max(range(len(pts)), key=lambda i: pts[i][1])
        east = max(range(len(pts)), key=lambda i: pts[i][2])
        self.assertEqual(max(range(len(verts)), key=lambda i: verts[i][1]), north)
        self.assertEqual(max(range(len(verts)), key=lambda i: verts[i][0]), east)

    def test_辺の長さは図面の距離の1000倍(self):
        _, polys = self.polylines()
        for (_, verts), cb in zip(polys, FIXTURE["chiban"]):
            for i, p in enumerate(cb["points"]):
                a, b = verts[i], verts[(i + 1) % len(verts)]
                self.assertAlmostEqual(math.hypot(b[0] - a[0], b[1] - a[1]), p[3] * 1000, delta=11)

    def test_原点firstは最初の点を0に置き全地番で共通(self):
        _, polys = self.polylines("first")
        self.assertEqual(polys[0][1][0], (0.0, 0.0))
        # K2 は K1 から 東へ 6.549m・北へ 12.355m
        self.assertAlmostEqual(polys[0][1][1][0], 6549.0, places=3)
        self.assertAlmostEqual(polys[0][1][1][1], 12355.0, places=3)
        # 道路の最初の点も同じ K1 なので 0,0
        self.assertEqual(polys[1][1][0], (0.0, 0.0))

    def test_原点を点名で指定できる(self):
        _, polys = self.polylines("R1")
        # 道路の3点目が R1
        self.assertEqual(polys[1][1][2], (0.0, 0.0))
        # K1 は R1 から 東へ 15.562m・北へ -2.324m
        self.assertAlmostEqual(polys[0][1][0][0], 15562.0, places=3)
        self.assertAlmostEqual(polys[0][1][0][1], -2324.0, places=3)

    def test_読み戻しで原点の取り違えを見つける(self):
        raw = sima_gen.build_dxf(data(), None).encode("cp932")
        self.assertEqual(sima_gen.readback_dxf(raw, data(), None), [])
        problems = sima_gen.readback_dxf(raw, data(), "first")
        self.assertTrue(any("位置が合わない" in p for p in problems), problems)

    def test_無い点名を原点にすると止まる(self):
        with self.assertRaises(sima_gen.InputError):
            sima_gen.build_dxf(data(), "無い点")

    def test_点名は共有点も1回だけ書く(self):
        pairs = sima_gen.dxf_pairs(sima_gen.build_dxf(data()).encode("cp932"))
        texts = [pairs[i + 1:i + 8] for i, p in enumerate(pairs) if p == (0, "TEXT")]
        names = [v for t in texts for c, v in t if c == 1]
        self.assertEqual(sorted(names), sorted(["K1", "K2", "Ｋ３", "K4", "R1", "R2", "1号地", "道路1"]))


class BadInput(unittest.TestCase):
    def with_point_name(self, name):
        d = fixture()
        d["chiban"][0]["points"][1][0] = name
        return d

    def test_改行やカンマや制御文字の入った名前は止まる(self):
        for name in ("K\r\n2", "K,2", "K\x002", " K2"):
            with self.assertRaises(sima_gen.InputError, msg=repr(name)):
                sima_gen.normalize(self.with_point_name(name))

    def test_CP932で書けない文字は止まる(self):
        with self.assertRaises(sima_gen.InputError):
            sima_gen.normalize(self.with_point_name("K\U0001F600"))

    def test_数値でない座標は止まる(self):
        for value in (True, "12.5", None, float("inf")):
            d = fixture()
            d["chiban"][0]["points"][1][1] = value
            with self.assertRaises(sima_gen.InputError, msg=repr(value)):
                sima_gen.normalize(d)

    def test_JSONのNaNは止まる(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nan.json"
            path.write_text('{"genba": "x", "chiban": [{"name": "a", "points": '
                            '[["P1", NaN, 0], ["P2", 1, 0], ["P3", 0, 1]]}]}', encoding="utf-8")
            with self.assertRaises(sima_gen.InputError):
                sima_gen.load_json(str(path))

    def test_最後に最初の点を繰り返すと止まる(self):
        d = fixture()
        d["chiban"][0]["points"].append(list(d["chiban"][0]["points"][0]))
        with self.assertRaises(sima_gen.InputError):
            sima_gen.normalize(d)

    def test_共有点の座標が食い違うと止まる(self):
        d = fixture()
        d["chiban"][1]["points"][0][1] += 0.01
        with self.assertRaises(sima_gen.InputError):
            sima_gen.normalize(d)

    def test_外周が8の字に交差していると止まる(self):
        d = fixture()
        pts = d["chiban"][0]["points"]
        pts[1], pts[2] = pts[2], pts[1]   # K2 と Ｋ３ の順を入れ替えると8の字になる
        for p in pts:
            p[3] = None
        d["chiban"][0]["area"] = None
        with self.assertRaises(sima_gen.InputError) as caught:
            sima_gen.normalize(d)
        self.assertIn("交差", str(caught.exception))

    def test_へこんだ形の敷地は通る(self):
        d = {"genba": "架空のL字地", "chiban": [{"name": "L字", "points": [
            ["A", 0.5, 0.5], ["B", 20.5, 0.5], ["C", 20.5, 10.5], ["D", 10.5, 10.5],
            ["E", 10.5, 25.5], ["F", 0.5, 25.5]]}]}
        self.assertEqual(len(sima_gen.normalize(d)["chiban"][0]["points"]), 6)

    def test_離れた2点が同じ位置だと止まる(self):
        d = {"genba": "x", "chiban": [{"name": "a", "points": [
            ["A", 0.5, 0.5], ["B", 10.5, 0.5], ["C", 10.5, 10.5], ["D", 0.5, 0.5], ["E", 0.5, 10.5]]}]}
        with self.assertRaises(sima_gen.InputError):
            sima_gen.normalize(d)

    def test_点が多すぎると止まる(self):
        with mock.patch.object(sima_gen, "MAX_POINTS", 5):
            with self.assertRaises(sima_gen.InputError):
                sima_gen.normalize(fixture())   # 共有点をまとめて6点

    def test_地番が無いと止まる(self):
        with self.assertRaises(sima_gen.InputError):
            sima_gen.normalize({"genba": "x", "chiban": []})


class Writing(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.dir = Path(self._temp.name)
        self.src = self.dir / "入力.json"
        self.src.write_text(json.dumps(fixture(), ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self._temp.cleanup()

    def test_既にある出力はoverwriteが無ければ触らない(self):
        old = self.dir / "入力.sim"
        old.write_bytes(b"old")
        code, out = run_tool(self.src, "--dxf")
        self.assertEqual(code, 2, out)
        self.assertEqual(old.read_bytes(), b"old")
        self.assertFalse((self.dir / "入力.dxf").exists())

    def test_出力先が入力と同じなら止まる(self):
        code, _ = run_tool(self.src, "--dxf-out", self.dir / "x.dxf", "-o", self.src)
        self.assertEqual(code, 2)

    def test_2つ目の置き換えに失敗したら1つ目も元に戻る(self):
        sim, dxf = self.dir / "a.sim", self.dir / "a.dxf"
        sim.write_bytes(b"old-sim")
        dxf.write_bytes(b"old-dxf")
        real = os.replace

        def flaky(src, dst):
            if str(dst) == str(dxf) and str(src).endswith(".tmp"):
                raise OSError("書けない")
            return real(src, dst)

        with mock.patch.object(sima_gen.os, "replace", side_effect=flaky):
            with self.assertRaises(sima_gen.WriteError) as caught:
                sima_gen.write_all([(str(sim), b"new-sim"), (str(dxf), b"new-dxf")],
                                   lambda path, raw: [], overwrite=True)
        self.assertEqual(caught.exception.stranded, [])
        self.assertEqual(sim.read_bytes(), b"old-sim")
        self.assertEqual(dxf.read_bytes(), b"old-dxf")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["a.dxf", "a.sim", "入力.json"])

    def test_新しく作った1つ目も2つ目の失敗で消える(self):
        sim, dxf = self.dir / "c.sim", self.dir / "c.dxf"
        dxf.write_bytes(b"old-dxf")
        real = os.replace

        def flaky(src, dst):
            if str(dst) == str(dxf) and str(src).endswith(".tmp"):
                raise OSError("書けない")
            return real(src, dst)

        with mock.patch.object(sima_gen.os, "replace", side_effect=flaky):
            with self.assertRaises(sima_gen.WriteError):
                sima_gen.write_all([(str(sim), b"new-sim"), (str(dxf), b"new-dxf")],
                                   lambda path, raw: [], overwrite=True)
        self.assertFalse(sim.exists())
        self.assertEqual(dxf.read_bytes(), b"old-dxf")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["c.dxf", "入力.json"])

    def test_元に戻せなかった時は退避先を消さずに知らせる(self):
        sim, dxf = self.dir / "d.sim", self.dir / "d.dxf"
        sim.write_bytes(b"old-sim")
        dxf.write_bytes(b"old-dxf")
        real = os.replace

        def flaky(src, dst):
            if str(dst) == str(dxf) and str(src).endswith(".tmp"):
                raise OSError("書けない")
            if str(dst) == str(sim) and str(src).endswith(".bak"):
                raise OSError("戻せない")
            return real(src, dst)

        with mock.patch.object(sima_gen.os, "replace", side_effect=flaky):
            with self.assertRaises(sima_gen.WriteError) as caught:
                sima_gen.write_all([(str(sim), b"new-sim"), (str(dxf), b"new-dxf")],
                                   lambda path, raw: [], overwrite=True)
        stranded = caught.exception.stranded
        self.assertEqual([e["path"] for e in stranded], [str(sim)])
        self.assertEqual(Path(stranded[0]["backup"]).read_bytes(), b"old-sim")
        self.assertEqual(dxf.read_bytes(), b"old-dxf")

    def test_確定の直前に出力先ができていたら上書きしない(self):
        sim = self.dir / "e.sim"

        def check(path, raw):
            sim.write_bytes(b"someone")   # 検査の間に別の誰かが同じ名前で作った
            return []

        with self.assertRaises(sima_gen.WriteError):
            sima_gen.write_all([(str(sim), b"new-sim")], check)
        self.assertEqual(sim.read_bytes(), b"someone")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["e.sim", "入力.json"])

    def test_読み戻し検査に落ちたら1つも書かない(self):
        sim, dxf = self.dir / "b.sim", self.dir / "b.dxf"
        problems = sima_gen.write_all([(str(sim), b"x"), (str(dxf), b"y")],
                                      lambda path, raw: ["だめ"] if path == str(dxf) else [])
        self.assertEqual(problems, ["だめ"])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["入力.json"])


if __name__ == "__main__":
    unittest.main()

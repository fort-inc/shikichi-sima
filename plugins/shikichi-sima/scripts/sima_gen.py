# -*- coding: utf-8 -*-
# 提供: 有限会社福井工務店（2026）
"""sima_gen — 測量図の座標から、CAD に読み込む敷地データ(SIMA .sim / DXF)を作る

測量図(丈量図・地積測量図)の座標求積表から書き写した JSON を読み、辺長と面積で検算してから書き出す。
**検算に落ちたら何も書かない。** 座標の写し間違いはここで止める。

SIMA の書式は ARCHITREND ZERO の「SIMA敷地読み込み」で実機合格した物を固定している。
変えるのは、実機で読み込みを確かめ直せる時だけ。

使い方:
    python "${CLAUDE_PLUGIN_ROOT}/scripts/sima_gen.py" 入力.json                     # 入力と同じ場所に 同名.sim
    python "${CLAUDE_PLUGIN_ROOT}/scripts/sima_gen.py" 入力.json --dxf               # 同名.sim と 同名.dxf
    python "${CLAUDE_PLUGIN_ROOT}/scripts/sima_gen.py" 入力.json --dxf --origin first   # DXF は最初の点を 0,0 に
    python "${CLAUDE_PLUGIN_ROOT}/scripts/sima_gen.py" 入力.json -o 出力.sim --dxf-out 出力.dxf
    python "${CLAUDE_PLUGIN_ROOT}/scripts/sima_gen.py" 入力.json --overwrite         # 既にある出力を置き換える

終了コード:
    0 書き出した
    1 検算か読み戻し検査に落ちた。何も書いていない
    2 入力の形・出力先が正しくない。何も書いていない
"""
import argparse
import json
import math
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

TOL_DIST = 0.011   # 辺長の許容差(m)。図面の距離欄は小数2桁なので丸め分を見込む
TOL_AREA = 0.02    # 面積の許容差(m2)
NAME_BYTES = 20    # 点名・地番名の桁(CP932 のバイト数)
MAX_XY = 1e7       # 座標の絶対値の上限(m)。平面直角座標は ±100万m 程度に収まる
MAX_POINTS = 9999  # 点の数・地番の数の上限。SIMA の番号欄は4桁
MAX_INPUT = 10 * 1024 * 1024   # 入力 JSON の大きさの上限(バイト)
MAX_Z = 1e5        # 標高の絶対値の上限(m)。%14.8f の桁に収まる範囲
DXF_MM = 1000.0    # DXF は mm で書く
TEXT_MM = 300.0    # DXF の文字の高さ(mm)
LAYER_LINE = "SHIKICHI"   # 敷地線
LAYER_POINT = "TENMEI"    # 点名
LAYER_LOT = "CHIBAN"      # 地番名


class InputError(Exception):
    """入力の形か出力先が正しくない。何も書かずに終了コード 2 で止まる。"""


# ---------------------------------------------------------------- 入力の検査

def check_text(label, s, max_bytes=None):
    """名前を検査する。CP932 で書けて、区切りの , と改行・制御文字を含まないこと。"""
    if not isinstance(s, str) or s == "":
        raise InputError("%s が空か、文字ではありません" % label)
    if s != s.strip():
        raise InputError("%s '%s' の前後に空白があります" % (label, s))
    for ch in s:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise InputError("%s '%s' に改行か制御文字が入っています" % (label, s.encode("unicode_escape").decode()))
    if "," in s:
        raise InputError("%s '%s' に , が入っています（SIMA の区切り文字なので使えません）" % (label, s))
    try:
        b = s.encode("cp932")
    except UnicodeEncodeError as e:
        raise InputError("%s の文字 '%s' は CP932 で表せません（SIMA は CP932 固定）"
                         % (label, s[e.start:e.end]))
    if max_bytes is not None and len(b) > max_bytes:
        raise InputError("%s '%s' は CP932 で %d バイトあり、%d 桁に収まりません（全角は1文字2バイト）"
                         % (label, s, len(b), max_bytes))
    return s


def check_number(label, v, limit, allow_none=False):
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise InputError("%s が数値ではありません: %r" % (label, v))
    v = float(v)
    if not math.isfinite(v) or abs(v) >= limit:
        raise InputError("%s の値が範囲外です: %r" % (label, v))
    return v


def normalize(data):
    """JSON を検査して、道具の内側で使う形にそろえる。

    点は (点名, X北, Y東, 次の点までの距離 or None, 標高) の組にする。
    """
    if not isinstance(data, dict):
        raise InputError("JSON の一番外側は {} にしてください")
    genba = check_text("現場名(genba)", data.get("genba"))
    lots = data.get("chiban")
    if not isinstance(lots, list) or not lots:
        raise InputError("地番(chiban)が1つもありません。SIMA は地番が無いと読み込めません")
    if len(lots) > MAX_POINTS:
        raise InputError("地番が %d 個を超えています" % MAX_POINTS)
    seen_lot, coord = set(), {}
    out = []
    for no, cb in enumerate(lots, start=1):
        if not isinstance(cb, dict):
            raise InputError("%d 番目の地番が {} の形になっていません" % no)
        name = check_text("地番名", cb.get("name"), NAME_BYTES)
        if name in seen_lot:
            raise InputError("地番名 '%s' が2回出てきます" % name)
        seen_lot.add(name)
        area = check_number("地番 '%s' の地積(area)" % name, cb.get("area"), 1e12, allow_none=True)
        if area is not None and area <= 0:
            raise InputError("地番 '%s' の地積が 0 以下です" % name)
        pts = cb.get("points")
        if not isinstance(pts, list) or len(pts) < 3:
            raise InputError("地番 '%s' の点が3つ未満です" % name)
        names_here, norm = set(), []
        for p in pts:
            if not isinstance(p, list) or not 3 <= len(p) <= 5:
                raise InputError("地番 '%s' の点 %r は [点名, X, Y, 距離, 標高] の形にしてください" % (name, p))
            nm = check_text("点名", p[0], NAME_BYTES)
            if nm in names_here:
                raise InputError("地番 '%s' に点 '%s' が2回出てきます（最後に最初の点を繰り返さないでください）"
                                 % (name, nm))
            names_here.add(nm)
            x = check_number("点 '%s' の X" % nm, p[1], MAX_XY)
            y = check_number("点 '%s' の Y" % nm, p[2], MAX_XY)
            d = check_number("点 '%s' の距離" % nm, p[3] if len(p) > 3 else None, 1e7, allow_none=True)
            if d is not None and d <= 0:
                raise InputError("点 '%s' の距離が 0 以下です" % nm)
            z = check_number("点 '%s' の標高" % nm, p[4] if len(p) > 4 else None, MAX_Z, allow_none=True)
            z = 0.0 if z is None else z
            if nm in coord:
                px, py, pz = coord[nm]
                if abs(px - x) > 1e-6 or abs(py - y) > 1e-6 or abs(pz - z) > 1e-6:
                    raise InputError("点名 '%s' が地番をまたいで別の座標で書かれています "
                                     "(%.3f,%.3f) と (%.3f,%.3f)" % (nm, px, py, x, y))
            else:
                coord[nm] = (x, y, z)
            norm.append((nm, x, y, d, z))
        at = {}
        for p in norm:
            key = (round(p[1], 6), round(p[2], 6))
            if key in at:
                raise InputError("地番 '%s' の点 '%s' と '%s' が同じ位置です" % (name, at[key], p[0]))
            at[key] = p[0]
        if shoelace(norm) < 1e-6:
            raise InputError("地番 '%s' の面積が 0 です（点が一直線に並んでいます）" % name)
        if self_intersects(local(norm)[0]):
            raise InputError("地番 '%s' の外周が交差しています（点の並び順を確かめてください）" % name)
        out.append({"name": name, "area": area, "points": norm})
    if len(coord) > MAX_POINTS:
        raise InputError("点が %d 個を超えています" % MAX_POINTS)
    return {"genba": genba, "chiban": out}


def load_json(path):
    def reject(word):
        raise InputError("JSON に %s が入っています" % word)
    try:
        if os.path.getsize(path) > MAX_INPUT:
            raise InputError("入力が大きすぎます（%d バイトまで）" % MAX_INPUT)
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f, parse_constant=reject)
    except OSError as e:
        raise InputError("入力を開けません: %s (%s)" % (path, e.strerror))
    except ValueError as e:
        raise InputError("JSON として読めません: %s" % e)


# ---------------------------------------------------------------- 検算

def local(points):
    """最初の点を原点にした座標の並び。測量座標は桁が大きいので、掛け算の前に引いて桁落ちを防ぐ。"""
    ox, oy = points[0][1], points[0][2]
    return [(p[1] - ox, p[2] - oy) for p in points], ox, oy


def shoelace(points):
    xy, _, _ = local(points)
    n = len(xy)
    return abs(math.fsum(xy[i][0] * xy[(i + 1) % n][1] - xy[(i + 1) % n][0] * xy[i][1]
                         for i in range(n))) / 2


def self_intersects(xy):
    """外周の辺どうしが交わっているか（8の字など）。隣り合う辺は端点を共有するので見ない。"""
    n = len(xy)

    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

    def on_segment(a, b, c):
        return (min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9
                and min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9)

    def crosses(p1, p2, p3, p4):
        d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
        d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
        if d1 * d2 < 0 and d3 * d4 < 0:
            return True
        return ((d1 == 0 and on_segment(p3, p4, p1)) or (d2 == 0 and on_segment(p3, p4, p2))
                or (d3 == 0 and on_segment(p1, p2, p3)) or (d4 == 0 and on_segment(p1, p2, p4)))

    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if crosses(xy[i], xy[(i + 1) % n], xy[j], xy[(j + 1) % n]):
                return True
    return False


def verify(name, points, expect_area):
    """シューレースで面積、隣接点間で辺長を検算。(面積, 全項目OKか) を返す。"""
    n = len(points)
    ok = True
    checked = 0
    for i in range(n):
        x1, y1 = points[i][1], points[i][2]
        x2, y2 = points[(i + 1) % n][1], points[(i + 1) % n][2]
        expect_d = points[i][3] if len(points[i]) > 3 and points[i][3] is not None else None
        if expect_d is not None:
            checked += 1
            d = math.hypot(x2 - x1, y2 - y1)
            if abs(d - expect_d) > TOL_DIST:
                print("  [NG] 辺長 %s->%s : 計算 %.3f / 図面 %.2f （差 %+.3f）"
                      % (points[i][0], points[(i + 1) % n][0], d, expect_d, d - expect_d))
                ok = False
    if checked == 0:
        print("  [--] 辺長 : 図面値の指定なし・未検算")
    elif ok:
        print("  [OK] 辺長 : %d本すべて許容内%s" % (checked, "" if checked == n else "（%d本は図面値なし・未検算）" % (n - checked)))
    area = shoelace(points)
    if expect_area is not None:
        diff = area - expect_area
        mark = "OK" if abs(diff) <= TOL_AREA else "NG"
        if mark == "NG":
            ok = False
        print("  [%s] 面積 : 計算 %.3f m2 / 図面 %.2f m2 （差 %+.3f）" % (mark, area, expect_area, diff))
    else:
        print("  [--] 面積 : 計算 %.3f m2 （図面値の指定なし・未検算）" % area)
    return area, ok


# ---------------------------------------------------------------- SIMA

def padb(s, n):
    """CP932のバイト長でn桁に左詰めパディング（全角混在対応）。

    f"{s:<20}" は「文字数」で数えるため全角が入ると桁がズレる。
    SIMAは桁位置で読まれるので必ずバイト長で揃える。
    """
    b = s.encode("cp932")
    if len(b) > n:
        raise ValueError("'%s' はCP932で%dバイトあり、%d桁に収まりません" % (s, len(b), n))
    return (b + b" " * (n - len(b))).decode("cp932")


def unique_points(data):
    """点名 -> 点番号。同名点は同一点として1度だけ数える（複数筆の共有点）。"""
    allpts, idx = [], {}
    for cb in data["chiban"]:
        for p in cb["points"]:
            if p[0] not in idx:
                idx[p[0]] = len(allpts) + 1
                allpts.append((p[0], p[1], p[2], p[4]))
    return allpts, idx


def build(data):
    """SIMA の中身(文字列・CRLF)を作る。書式は実機合格した物のまま。"""
    allpts, idx = unique_points(data)
    L = ["G00,01,%s," % data["genba"], "A00,"]
    for nm, x, y, z in allpts:
        L.append("A01,%4d,%s,%18.8f,%18.8f,%14.8f," % (idx[nm], padb(nm, NAME_BYTES), x, y, z))
    L.append("A99,")
    for no, cb in enumerate(data["chiban"], start=1):
        L.append("D00,%4d,%s,1," % (no, padb(cb["name"], NAME_BYTES)))
        for p in cb["points"]:
            L.append("B01,%4d,%s," % (idx[p[0]], padb(p[0], NAME_BYTES)))
        L.append("D99,")
    L.append("G99,")
    return "\r\n".join(L) + "\r\n", allpts


def readback_sim(raw, data):
    """書いた .sim のバイト列を読み戻して確かめる（桁ズレ・文字化け・中身の取り違えの検出）。"""
    allpts, idx = unique_points(data)
    n_chiban, n_pts = len(data["chiban"]), len(allpts)
    problems = []
    try:
        t = raw.decode("cp932")
    except UnicodeDecodeError:
        return ["CP932 として読めない"]
    lines = t.split("\r\n")
    if lines[-1] != "" or "\n" in t.replace("\r\n", ""):
        problems.append("改行が CRLF にそろっていない")
    lines = lines[:-1]
    heads = [ln[:4] for ln in lines]
    if heads.count("A01,") != n_pts:
        problems.append("A01行数 %d != 点数 %d" % (heads.count("A01,"), n_pts))
    if heads.count("D00,") != n_chiban:
        problems.append("D00行数 %d != 地番数 %d" % (heads.count("D00,"), n_chiban))
    if not t.startswith("G00,01,"):
        problems.append("G00で始まっていない")
    if not t.rstrip("\r\n").endswith("G99,"):
        problems.append("G99で終わっていない")
    for ln in lines:
        if ln.startswith(("A01,", "B01,", "D00,")):
            # 点番号4桁 + 点名/地番名20バイト の桁位置を検査
            b = ln.encode("cp932")
            if b[3:4] != b"," or b[8:9] != b"," or b[29:30] != b",":
                problems.append("桁位置ズレ: %s" % ln)
                break
    # 中身の照合。A01 は番号・点名・座標を桁位置で読み、元の点と比べる
    a01 = [ln.encode("cp932") for ln in lines if ln.startswith("A01,")]
    for b, (nm, x, y, z) in zip(a01, allpts):
        try:
            same = (int(b[4:8]) == idx[nm] and b[9:29].decode("cp932").rstrip(" ") == nm
                    and abs(float(b[30:48]) - x) < 1e-7 and abs(float(b[49:67]) - y) < 1e-7
                    and abs(float(b[68:82]) - z) < 1e-7)
        except (ValueError, UnicodeDecodeError):
            same = False
        if not same:
            problems.append("A01 の中身が元の点と合わない: %s" % b.decode("cp932", "replace"))
            break
    # D00 ごとの B01 の並びが、地番ごとの点の並びと同じか
    found, cur = [], None
    for ln in lines:
        if ln.startswith("D00,"):
            cur = []
            found.append((ln, cur))
        elif ln.startswith("B01,") and cur is not None:
            cur.append(ln)
        elif ln.startswith("D99,"):
            cur = None
    expect = [("D00,%4d,%s,1," % (no, padb(cb["name"], NAME_BYTES)),
               ["B01,%4d,%s," % (idx[p[0]], padb(p[0], NAME_BYTES)) for p in cb["points"]])
              for no, cb in enumerate(data["chiban"], start=1)]
    if found != expect:
        problems.append("地番ごとの点の並び(D00/B01)が元と合わない")
    return problems


# ---------------------------------------------------------------- DXF

def resolve_origin(data, origin):
    """DXF の原点にする測量座標 (X0, Y0) を返す。

    None は移動なし。first は1つ目の地番の1つ目の点（全地番で同じ原点を使う）。
    それ以外は点名として探す。
    """
    if origin is None:
        return 0.0, 0.0
    allpts, idx = unique_points(data)
    if origin == "first":
        p = data["chiban"][0]["points"][0]
        return p[1], p[2]
    if origin not in idx:
        raise InputError("原点にする点 '%s' がありません" % origin)
    p = allpts[idx[origin] - 1]
    return p[1], p[2]


def to_dxf(x, y, x0, y0):
    """測量座標(X=北, Y=東, m) → DXF 座標(x=東, y=北, mm)。原点を先に引いてから mm にする。"""
    return (y - y0) * DXF_MM, (x - x0) * DXF_MM


def centroid(points):
    """多角形の重心(測量座標)。地番名の文字を置く位置に使う。"""
    xy, ox, oy = local(points)
    n = len(xy)
    a, cx, cy = [], [], []
    for i in range(n):
        (x1, y1), (x2, y2) = xy[i], xy[(i + 1) % n]
        c = x1 * y2 - x2 * y1
        a.append(c)
        cx.append((x1 + x2) * c)
        cy.append((y1 + y2) * c)
    a = math.fsum(a)
    return ox + math.fsum(cx) / (3 * a), oy + math.fsum(cy) / (3 * a)


def build_dxf(data, origin=None):
    """DXF R12 の中身(文字列・CRLF)を作る。敷地線は地番ごとの閉じた POLYLINE、点名と地番名は TEXT。"""
    x0, y0 = resolve_origin(data, origin)
    allpts, _ = unique_points(data)
    xy = [to_dxf(x, y, x0, y0) for _, x, y, _ in allpts]
    minx, maxx = min(p[0] for p in xy), max(p[0] for p in xy)
    miny, maxy = min(p[1] for p in xy), max(p[1] for p in xy)

    out = []

    def g(code, value):
        if isinstance(value, float):
            value = "%.4f" % value
        out.append("%3d" % code)
        out.append(str(value))

    g(0, "SECTION"); g(2, "HEADER")
    g(9, "$ACADVER"); g(1, "AC1009")
    g(9, "$DWGCODEPAGE"); g(3, "ANSI_932")
    g(9, "$INSUNITS"); g(70, 4)
    g(9, "$EXTMIN"); g(10, minx); g(20, miny); g(30, 0.0)
    g(9, "$EXTMAX"); g(10, maxx); g(20, maxy); g(30, 0.0)
    g(0, "ENDSEC")

    g(0, "SECTION"); g(2, "TABLES")
    g(0, "TABLE"); g(2, "LTYPE"); g(70, 1)
    g(0, "LTYPE"); g(2, "CONTINUOUS"); g(70, 0); g(3, "Solid line"); g(72, 65); g(73, 0); g(40, 0.0)
    g(0, "ENDTAB")
    layers = (("0", 7), (LAYER_LINE, 1), (LAYER_POINT, 7), (LAYER_LOT, 5))
    g(0, "TABLE"); g(2, "LAYER"); g(70, len(layers))
    for name, color in layers:
        g(0, "LAYER"); g(2, name); g(70, 0); g(62, color); g(6, "CONTINUOUS")
    g(0, "ENDTAB")
    g(0, "ENDSEC")

    g(0, "SECTION"); g(2, "ENTITIES")
    for cb in data["chiban"]:
        g(0, "POLYLINE"); g(8, LAYER_LINE); g(66, 1)
        g(10, 0.0); g(20, 0.0); g(30, 0.0); g(70, 1)
        for p in cb["points"]:
            vx, vy = to_dxf(p[1], p[2], x0, y0)
            g(0, "VERTEX"); g(8, LAYER_LINE); g(10, vx); g(20, vy); g(30, 0.0)
        g(0, "SEQEND"); g(8, LAYER_LINE)
    for (nm, _, _, _), (px, py) in zip(allpts, xy):
        g(0, "TEXT"); g(8, LAYER_POINT); g(10, px); g(20, py); g(30, 0.0); g(40, TEXT_MM); g(1, nm)
    for cb in data["chiban"]:
        cx, cy = centroid(cb["points"])
        tx, ty = to_dxf(cx, cy, x0, y0)
        g(0, "TEXT"); g(8, LAYER_LOT); g(10, tx); g(20, ty); g(30, 0.0); g(40, TEXT_MM); g(1, cb["name"])
    g(0, "ENDSEC")
    g(0, "EOF")
    return "\r\n".join(out) + "\r\n"


def dxf_pairs(raw):
    """DXF のバイト列を (グループコード, 値) の並びに戻す。"""
    lines = raw.decode("cp932").split("\r\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if len(lines) % 2:
        raise ValueError("グループコードと値が2行1組になっていない")
    return [(int(lines[i]), lines[i + 1]) for i in range(0, len(lines), 2)]


def dxf_polylines(pairs):
    """POLYLINE ごとに (閉じているか, [(x, y), ...]) を返す。"""
    result, cur, ent, i = [], None, None, 0
    while i < len(pairs):
        code, value = pairs[i]
        if code == 0:
            ent = value
            if value == "POLYLINE":
                cur = [False, []]
                result.append(cur)
            elif value == "VERTEX" and cur is not None:
                cur[1].append([None, None])
            elif value == "SEQEND":
                cur = None
        elif ent == "POLYLINE" and code == 70 and cur is not None:
            cur[0] = bool(int(value) & 1)
        elif ent == "VERTEX" and cur is not None and code in (10, 20):
            cur[1][-1][0 if code == 10 else 1] = float(value)
        i += 1
    return [(closed, [tuple(v) for v in verts]) for closed, verts in result]


def readback_dxf(raw, data, origin=None):
    """書いた .dxf を読み戻して、区切り・画層・閉合・頂点の数と位置・文字の数を確かめる。

    これは道具の内側の構造検査で、CAD で読み込めることの保証ではない。
    """
    try:
        pairs = dxf_pairs(raw)
    except (UnicodeDecodeError, ValueError) as e:
        return ["DXF として読めない: %s" % e]
    problems = []
    if pairs[:2] != [(0, "SECTION"), (2, "HEADER")]:
        problems.append("SECTION HEADER で始まっていない")
    if pairs[-1:] != [(0, "EOF")]:
        problems.append("EOF で終わっていない")
    if sum(1 for p in pairs if p == (0, "SECTION")) != sum(1 for p in pairs if p == (0, "ENDSEC")):
        problems.append("SECTION と ENDSEC の数が合わない")
    for i in range(len(pairs) - 1):
        if pairs[i] == (0, "SECTION") and pairs[i + 1][0] != 2:
            problems.append("SECTION の直後に名前が無い")
    for layer in (LAYER_LINE, LAYER_POINT, LAYER_LOT):
        if not any(pairs[i] == (0, "LAYER") and pairs[i + 1] == (2, layer) for i in range(len(pairs) - 1)):
            problems.append("画層 %s が定義されていない" % layer)
    allpts, _ = unique_points(data)
    texts = sum(1 for p in pairs if p == (0, "TEXT"))
    if texts != len(allpts) + len(data["chiban"]):
        problems.append("文字 %d 個 != 点 %d + 地番 %d" % (texts, len(allpts), len(data["chiban"])))
    x0, y0 = resolve_origin(data, origin)
    polys = dxf_polylines(pairs)
    if len(polys) != len(data["chiban"]):
        problems.append("POLYLINE %d 本 != 地番 %d" % (len(polys), len(data["chiban"])))
    for (closed, verts), cb in zip(polys, data["chiban"]):
        if not closed:
            problems.append("地番 '%s' の敷地線が閉じていない" % cb["name"])
        if len(verts) != len(cb["points"]):
            problems.append("地番 '%s' の頂点 %d != 点 %d" % (cb["name"], len(verts), len(cb["points"])))
            continue
        for (vx, vy), p in zip(verts, cb["points"]):
            ex, ey = to_dxf(p[1], p[2], x0, y0)
            if vx is None or vy is None or abs(vx - ex) > 0.001 or abs(vy - ey) > 0.001:
                problems.append("地番 '%s' の点 '%s' の位置が合わない" % (cb["name"], p[0]))
                break
    return problems


# ---------------------------------------------------------------- 書き出し

class WriteError(Exception):
    """書き出しに失敗した。stranded には、元に戻せなかった出力の様子が入る。"""

    def __init__(self, message, stranded=()):
        Exception.__init__(self, message)
        self.stranded = list(stranded)


def _rollback(done):
    """置き換えの途中まで進んだ物を、前の状態に戻す。戻せなかった物を返す。"""
    stranded = []
    for e in reversed(done):
        try:
            if e["moved"]:
                os.replace(e["backup"], e["path"])
            else:
                if e["backup"]:
                    os.remove(e["backup"])
                if e["placed"]:
                    os.remove(e["path"])
        except OSError:
            stranded.append(e)
    return stranded


def write_all(outputs, check, overwrite=False):
    """出力をそろって書くか、1つも書かないかのどちらかにする。

    1 全部を出力先と同じフォルダの一時ファイルに書く
    2 一時ファイルを読み戻して check(パス, バイト列) で検査する。問題があれば一時ファイルを消して返す
    3 上書きしてよいと言われていないのに、出力先にファイルが現れていたら止める
    4 置き換える。前の物は退避しておき、途中で失敗したら全部を前の状態に戻す
    戻り値は問題の一覧（空なら書けた）。置き換えで失敗したら WriteError を投げる。
    電源が落ちるような、道具そのものが止まる失敗までは面倒を見ない。
    """
    temps = []
    try:
        try:
            for path, data in outputs:
                fd, tmp = tempfile.mkstemp(prefix=".sima_gen_", suffix=".tmp",
                                           dir=os.path.dirname(os.path.abspath(path)))
                temps.append(tmp)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
        except OSError as e:
            raise WriteError("一時ファイルを書けません（%s）" % e)
        problems = []
        for (path, _), tmp in zip(outputs, temps):
            with open(tmp, "rb") as f:
                problems += check(path, f.read())
        if problems:
            return problems
        if not overwrite:
            appeared = [path for path, _ in outputs if os.path.exists(path)]
            if appeared:
                raise WriteError("書き出しの途中で、出力先にファイルができました: %s" % ", ".join(appeared))
        done = []
        try:
            for (path, _), tmp in zip(outputs, temps):
                entry = {"path": path, "backup": None, "moved": False, "placed": False}
                done.append(entry)
                if os.path.exists(path):
                    fd, entry["backup"] = tempfile.mkstemp(prefix=".sima_gen_", suffix=".bak",
                                                           dir=os.path.dirname(os.path.abspath(path)))
                    os.close(fd)
                    os.replace(path, entry["backup"])
                    entry["moved"] = True
                os.replace(tmp, path)
                entry["placed"] = True
        except OSError as e:
            raise WriteError("置き換えに失敗しました（%s）" % e, _rollback(done))
        for entry in done:
            if entry["backup"]:
                try:
                    os.remove(entry["backup"])
                except OSError:
                    print("[WARN] 前の出力の退避を消せませんでした。不要なら消してください: %s"
                          % entry["backup"])
        return []
    finally:
        for tmp in temps:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


def same_file(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def plan_outputs(args):
    """出力先を決めて確かめる。(sim のパス, dxf のパス or None)。"""
    stem = os.path.splitext(args.input)[0]
    sim_path = args.output or stem + ".sim"
    dxf_path = args.dxf_out or (stem + ".dxf" if args.dxf else None)
    if not sim_path.lower().endswith(".sim"):
        raise InputError("SIMA の出力先は .sim で終わる名前にしてください: %s" % sim_path)
    if dxf_path and not dxf_path.lower().endswith(".dxf"):
        raise InputError("DXF の出力先は .dxf で終わる名前にしてください: %s" % dxf_path)
    if args.origin is not None and not dxf_path:
        raise InputError("--origin は DXF を作る時(--dxf)だけ使えます")
    paths = [sim_path] + ([dxf_path] if dxf_path else [])
    for p in paths:
        folder = os.path.dirname(os.path.abspath(p))
        if not os.path.isdir(folder):
            raise InputError("出力先のフォルダがありません: %s" % folder)
        if same_file(p, args.input):
            raise InputError("出力先が入力と同じです: %s" % p)
        if os.path.exists(p) and not os.path.isfile(p):
            raise InputError("出力先がファイルではありません: %s" % p)
    if dxf_path and same_file(sim_path, dxf_path):
        raise InputError("SIMA と DXF の出力先が同じです")
    existing = [p for p in paths if os.path.exists(p)]
    if existing and not args.overwrite:
        raise InputError("出力先に既にファイルがあります: %s（置き換えるなら --overwrite）"
                         % ", ".join(existing))
    return sim_path, dxf_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="測量図の座標から、検算付きで SIMA(.sim)／DXF を作る")
    ap.add_argument("input", help="座標を書き写した JSON")
    ap.add_argument("-o", "--output", help="SIMA の出力先（既定: 入力と同じ場所・同名 .sim）")
    ap.add_argument("--dxf", action="store_true", help="DXF も作る（既定: 入力と同じ場所・同名 .dxf）")
    ap.add_argument("--dxf-out", help="DXF の出力先（指定すると --dxf を付けたのと同じ）")
    ap.add_argument("--origin", help="DXF の原点にする点。first=最初の点／点名。既定は測量座標のまま")
    ap.add_argument("--overwrite", action="store_true", help="既にある出力を置き換える")
    args = ap.parse_args(argv)

    try:
        data = normalize(load_json(args.input))
        sim_path, dxf_path = plan_outputs(args)
        if dxf_path:
            resolve_origin(data, args.origin)
    except InputError as e:
        print("[ERROR] %s" % e)
        print("何も書いていません。")
        return 2

    print("=== 検算（辺長 許容%.3fm / 面積 許容%.2fm2）===" % (TOL_DIST, TOL_AREA))
    all_ok = True
    for cb in data["chiban"]:
        print("地番 [%s] 点%d" % (cb["name"], len(cb["points"])))
        _, ok = verify(cb["name"], cb["points"], cb["area"])
        all_ok = all_ok and ok

    if not all_ok:
        print("\n[STOP] 検算に落ちました。何も書いていません。座標の写し間違いを疑ってください。")
        print("       図面の値そのものが食い違っている時は、測量した事務所に確かめてください。")
        for p in (sim_path, dxf_path):
            if p and os.path.exists(p):
                print("       ※ %s は前に作った物のままです。今回の結果ではありません。" % p)
        return 1

    text, allpts = build(data)
    outputs = [(sim_path, text.encode("cp932"))]
    if dxf_path:
        outputs.append((dxf_path, build_dxf(data, args.origin).encode("cp932")))

    def check(path, raw):
        if path == sim_path:
            return readback_sim(raw, data)
        return readback_dxf(raw, data, args.origin)

    try:
        problems = write_all(outputs, check, args.overwrite)
    except WriteError as e:
        print("\n[ERROR] 書き出しに失敗しました: %s" % e)
        if not e.stranded:
            print("何も書いていません。前からあった物は元のままです。")
        for st in e.stranded:
            if st["moved"]:
                print("   - 前の %s は %s に退避したままです。名前を戻してください。" % (st["path"], st["backup"]))
            else:
                print("   - %s は今回の物が残っています。消してください。" % st["path"])
        return 2
    if problems:
        print("\n[NG] 読み戻し検査で問題があったので、何も書いていません:")
        for p in problems:
            print("   -", p)
        return 1

    print("\n[OK] 読み戻し検査 合格（書いた中身の形の検査。CAD で読み込めることの保証ではない）")
    print("SIMA: %s（%dバイト, CP932/CRLF, 点%d, 地番%d）"
          % (sim_path, len(outputs[0][1]), len(allpts), len(data["chiban"])))
    if dxf_path:
        where ="測量座標のまま" if args.origin is None else "点 %s を 0,0 に移動" % (
            data["chiban"][0]["points"][0][0] if args.origin == "first" else args.origin)
        print("DXF : %s（R12, 単位 mm, x=東 y=北, %s）" % (dxf_path, where))
    print("\nARCHITREND ZERO: 「敷地」→「敷地」→「SIMA敷地読み込み」で読み込み、ダイアログで地番を選ぶ")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# shikichi-sima

測量図(丈量図・地積測量図)の座標求積表から、CAD に読み込む敷地データ(SIMA .sim／DXF)を作る Claude Code プラグインです。

座標の写し間違いを防ぐため、2段で止めます。

- 読み取った座標の表を使う人に見せ、「合ってる」と返るまでファイルを書きません。
- 辺の長さと面積を図面の値と照らし、1つでも合わなければ何も書き出しません。

実機で確かめた CAD は ARCHITREND ZERO(SIMA 読み込み)だけです。ほかの CAD と DXF の読み込みは未確認です。

## 入れ方

ターミナルで次の2行を順に打ちます。

```
claude plugin marketplace add fort-inc/shikichi-sima
claude plugin install shikichi-sima@shikichi-sima
```

Claude Code のセッションの中なら、次の3行を順に打っても同じです。

```
/plugin marketplace add fort-inc/shikichi-sima
/plugin install shikichi-sima@shikichi-sima
/reload-plugins
```

チームの全員に入れたい時は、共有しているフォルダの `.claude/settings.json` に次を書きます。フォルダを信頼した時点で、コマンド無しで入ります。

```json
{
  "extraKnownMarketplaces": {
    "shikichi-sima": {
      "source": { "source": "github", "repo": "fort-inc/shikichi-sima" }
    }
  },
  "enabledPlugins": {
    "shikichi-sima@shikichi-sima": true
  }
}
```

## 使い方

Claude Code に「測量図から敷地データ作って」と言って、測量図の PDF か画像を渡します。「SIMA作って」「敷地を CAD に入れたい」でも動きます。

必要なもの: Python 3.9 以上(標準ライブラリのみ)。

## 新しい版に更新する

```
claude plugin marketplace update shikichi-sima
claude plugin update shikichi-sima@shikichi-sima
```

## 外す

```
claude plugin uninstall shikichi-sima@shikichi-sima
```

書き出した `.json`・`.sim`・`.dxf` の他には、何も残しません。

## テスト

架空の土地だけを使うテストが入っています。

```
python -m pytest plugins/shikichi-sima/tests -q
```

## ライセンス

MIT(`LICENSE` を参照)。提供: 有限会社福井工務店(2026)。

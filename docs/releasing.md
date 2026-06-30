# リリース運用ガイド

loop-agent を PyPI へリリースする手順と方針をまとめる。発行は GitHub Actions
（[`.github/workflows/release.yml`](../.github/workflows/release.yml)）が
**OIDC Trusted Publishing** で自動実行する。API token も secrets も使わない。

## バージョニング方針（SemVer）

[Semantic Versioning](https://semver.org/lang/ja/) に従い `MAJOR.MINOR.PATCH`
を付ける。

- **MAJOR**: 後方互換を壊す変更（公開 API の削除・改名・シグネチャ変更、永続
  スキーマの非互換変更など）。
- **MINOR**: 後方互換を保つ機能追加（新しい公開 API・新 extra・新オプション）。
- **PATCH**: 後方互換を保つ bug fix・ドキュメント・内部改善。

### 0.x 系の注意

`0.y.z` の間は公開 API が安定保証の対象外である。実務上は次の運用とする:

- 破壊変更は MINOR（`0.y` の `y`）を上げる。
- 機能追加・bug fix は PATCH（`z`）を上げる。
- 公開 API が安定したら `1.0.0` を切り、以後は厳密な SemVer に移行する。

「公開 API」とは `loop_agent/__init__.py` の `__all__` でエクスポートされる
シンボルを指す。

### 1.0.0 以降の互換性

`1.0.0` 以降は [stability.md](./stability.md) を安定契約の正本とする。

- 公開 API の削除・改名・非互換なシグネチャ変更は MAJOR。
- 後方互換の機能追加・新 option・新 helper は MINOR。
- bug fix・docs・内部改善・後方互換な metadata 修正は PATCH。
- CLI のサブコマンド名・終了コード・TOML の主要キーは安定契約に含む。
- state.db の非破壊 migration は MINOR/PATCH、既存 DB の読み取り互換を壊す変更は MAJOR。

破壊変更は、可能な限り minor release で deprecation を告知し、後続 major release で削除する。安全性や正しさのために旧挙動を維持できない場合は、CHANGELOG に理由と移行手順を書く。

### 1.0.0 release gate

`1.0.0` を切る前に次を満たす:

- README から [stability.md](./stability.md) が辿れる。
- `pyproject.toml` classifier が `Development Status :: 5 - Production/Stable`。
- `pyproject.toml` / `loop_agent.__version__` / `CHANGELOG.md` / tag が同一 version。
- `python -m pytest` が pass。
- `python -m build` が pass。
- `python -m twine check dist/*` が pass。
- `python scripts/verify_wheel_skill_bundle.py` が pass。

## 単一の version source

version は 2 箇所に書く。リリース前に**必ず一致**させる:

1. [`pyproject.toml`](../pyproject.toml) の `[project].version`
   （ビルド成果物 = wheel/sdist の版になる。タグが publish する版はこれ）
2. [`src/loop_agent/__init__.py`](../src/loop_agent/__init__.py) の `__version__`

`git tag` の `vX.Y.Z` と上記 2 つの `X.Y.Z` を揃える。タグの版と pyproject の
版が食い違うと、タグ名と異なる版が publish される事故になる。

## リリース手順

1. **version bump**: `pyproject.toml` と `__init__.py` の version を新しい
   `X.Y.Z` に更新する。
2. **CHANGELOG 更新**: [`CHANGELOG.md`](../CHANGELOG.md) の `[Unreleased]` の
   内容を `[X.Y.Z] - YYYY-MM-DD` セクションへ移し、日付を確定する。新しい空の
   `[Unreleased]` を残し、末尾のリンク定義（compare URL）も更新する。
3. **PR 作成 -> レビュー -> `main` マージ**: 上記をまとめた PR を出し、CI
   （[`ci.yml`](../.github/workflows/ci.yml)）が green であることを確認して
   `main` へマージする。
4. **タグ push**: `main` の該当コミットに `vX.Y.Z` タグを打って push する。

   ```bash
   git checkout main && git pull
   git tag v1.0.0
   git push origin v1.0.0
   ```

   これは**人間が判断して行う最終ゲート**。タグ push が publish の引き金になる。
5. **自動 publish**: `v*` タグ push で `release.yml` が起動し、`python -m build`
   -> `twine check` -> PyPI publish を実行する。
6. **確認**: PyPI のページ（https://pypi.org/project/loop-agent/）に新版が出た
   ことと、GitHub Actions のジョブが成功したことを確認する。

### リリース前のローカル検証

タグを打つ前に、ワークフローと同じ検証をローカルで実行できる:

```bash
python -m pip install --upgrade build twine   # もしくは: pip install -e .[dev]
python -m build                                # dist/ に wheel と sdist を生成
python -m twine check dist/*                   # メタデータ / long description を検証
```

`twine check` は README（long description）が PyPI で正しく描画されるかも検証
する。`readme = "README.md"` のため content-type は `text/markdown` として
自動付与される。

## OIDC Trusted Publishing の仕組み

PyPI への発行は **OIDC（OpenID Connect）Trusted Publishing** で行う。長期 API
token を持たず、**secrets を一切リポジトリに置かない**のが要点。

仕組みの流れ:

1. PyPI 側で「この PyPI プロジェクトは、この GitHub リポジトリのこのワークフロー
   からの発行を信頼する」という **trusted publisher** をあらかじめ登録しておく
   （リポジトリ・ワークフローファイル名・environment を指定）。
2. ワークフロー実行時、GitHub Actions が短命の **OIDC token**（実行元の
   リポジトリ・ワークフロー・ref などを証明する署名付き JWT）を発行する。
3. `pypa/gh-action-pypi-publish` がその OIDC token を PyPI に提示し、PyPI は
   登録済み trusted publisher と突き合わせて検証し、**その場限りの短命な発行
   権限**を返す。
4. その短命権限で wheel/sdist を upload する。token はジョブ終了とともに失効する。

そのために `release.yml` のジョブには次の permission が必要:

```yaml
permissions:
  id-token: write   # OIDC token をワークフローに発行させる（Trusted Publishing の核）
  contents: read
```

`id-token: write` が無いと OIDC token を取得できず発行に失敗する。逆に、これが
あれば PyPI 用の API token を secrets に置く必要はない。

> NOTE: trusted publisher の登録は PyPI 側の人手設定であり、リポジトリのコード
> では完結しない。新規プロジェクトや publisher 設定変更時は PyPI の
> "Publishing" 設定で対象ワークフローが登録済みか確認する。

## インシデント対応

### yank（公開済み版の取り下げ）

壊れた版を公開してしまった場合、PyPI からファイルを**削除するのではなく
yank する**。yank された版は、明示的にその版を pin した既存利用者には引き続き
解決されるが、新規の `pip install loop-agent` の解決候補からは除外される。

- 手順: PyPI のプロジェクト管理画面（Manage -> Releases）で対象版を yank する。
- 同じ版番号での再 upload はできない（PyPI は版の上書きを許さない）。修正版は
  次の PATCH（例: `0.1.1`）として出し直す。

### 緊急修正（hotfix）

1. `main` から `fix/...` ブランチを切り、最小の修正を入れる。
2. PATCH を上げる（例: `0.1.0` -> `0.1.1`）。CHANGELOG に `Fixed` を記載。
3. 通常のリリース手順（PR -> マージ -> タグ push）で publish する。
4. 旧版に深刻な不具合がある場合は、上記の通り旧版を yank して新版へ誘導する。

### publish 失敗時

GitHub Actions の `release.yml` ジョブログを確認する。よくある原因:

- `twine check` の失敗（メタデータ / long description 不正） -> 修正して再リリース。
- OIDC の失敗（`id-token: write` 欠落 / PyPI 側 trusted publisher 未登録 /
  リポジトリ・ワークフロー名の不一致） -> permission と PyPI 設定を確認。

PyPI への upload 前にジョブが落ちた場合は版が publish されていないため、修正後に
同じタグを打ち直せる（既存タグを消してから再 push する）。upload まで進んでいた
場合は版が確定しているので、yank + 次 PATCH で対応する。

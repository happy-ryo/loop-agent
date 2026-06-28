# tests/adapters を 1 つのパッケージにして、この配下の ``conftest`` がトップレベル
# ``tests/conftest.py`` (ループコア共通ヘルパ) と **モジュール名衝突しない** ように
# する。pytest の prepend import mode では、__init__.py が無いと両 conftest がともに
# モジュール名 ``conftest`` になり、ルートの ``from conftest import ...`` がこちらの
# アダプタ用 conftest を誤って拾う。__init__.py を置くとこの配下は ``adapters.*``
# として import され、衝突を避けられる。

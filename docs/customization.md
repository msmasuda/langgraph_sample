# 拡張 & カスタマイズガイド

本ドキュメントでは、LangGraphエージェントに新しいツールを追加したり、モデル設定やメモリ永続化層を拡張する方法を解説します。

---

## 1. 新しいツールの追加手順

ツールは `@tool` デコレータ（`langchain_core.tools`）を使用することで簡単に追加できます。

### ステップ 1: `src/tools.py` に関数を定義

関数のdocstring（説明文）と型ヒントは、LLMがツールを選択・引数を生成する際の重要な判断材料となります。

```python
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """指定された都市の現在の天気情報を取得します。

    Args:
        city: 都市名 (例: 'Tokyo', 'Osaka')

    Returns:
        天気情報の要約文字列
    """
    # 外部API呼び出し等の実装
    return f"{city}の現在の天気は晴れ、気温は22℃です。"
```

### ステップ 2: `ALL_TOOLS` に追加

`src/tools.py` 末尾のリスト `ALL_TOOLS` に定義したツールを追加します。

```python
ALL_TOOLS = [
    get_current_datetime,
    calculator,
    web_search,
    save_note,
    read_notes,
    get_weather,  # ← ここに追加
]
```

これだけで、エージェント（CLI・Web UIともに）が自動的に新ツールを認識し、適切なタイミングで呼び出すようになります。

---

## 2. システムプロンプトのカスタマイズ

エージェントの性格や専門性、ルールを変更したい場合、`.env` または `src/config.py` でシステムプロンプトを変更できます。

### 方法 1: `.env` で設定
```ini
SYSTEM_PROMPT="あなたはプログラミング専門のアシスタントです。コード例を交えて簡潔に回答してください。"
```

### 方法 2: `create_agent` 呼び出し時に引数で指定
```python
from src.agent import create_agent

agent = create_agent(
    model_name="qwen3.5:9b-mlx",
    system_prompt="あなたはデータサイエンスの専門家です。"
)
```

---

## 3. Ollama モデルの追加・切り替え

### 新しいモデルのダウンロード (Ollama CLI)
```bash
ollama pull llama3.3:70b
ollama pull deepseek-r1:14b
```

### アプリケーションでの利用
- **`.env`**: `OLLAMA_MODEL=deepseek-r1:14b`
- **CLI**: `/model deepseek-r1:14b`
- **Web UI**: サイドバーのモデル入力欄に直接入力

---

## 4. チェックポインタの永続化拡張 (SQLite / PostgreSQL)

デフォルトでは`AsyncCompatibleSqliteSaver`を使用し、`data/checkpoints.sqlite`へ対話履歴を永続化しています。同期実行と非同期実行の両方から同じ履歴を利用できます。本番の複数プロセス・複数サーバー構成では、PostgreSQL用チェックポインタへの移行を推奨します。

### SQLite永続化の例:

```python
import sqlite3
from src.agent import AsyncCompatibleSqliteSaver

# SQLiteデータベース接続
conn = sqlite3.connect("data/checkpoints.sqlite", check_same_thread=False)
checkpointer = AsyncCompatibleSqliteSaver(conn)

# エージェント構築時に渡す
agent = create_agent(checkpointer=checkpointer)
```

APIでは`CHECKPOINT_DATABASE_URL`を設定すると`AsyncPostgresSaver`を利用します。アプリ用の接続はSQLAlchemy asyncpg形式、LangGraph用はpsycopg形式で指定します。

```ini
DATABASE_URL=postgresql+asyncpg://langgraph:パスワード@192.168.100.2:5432/langgraph
CHECKPOINT_DATABASE_URL=postgresql://langgraph:パスワード@192.168.100.2:5432/langgraph
```

スキーマ変更時はAlembicの新しいリビジョンを作成し、`uv run alembic upgrade head`で適用します。LangGraph所有テーブルをアプリのAlembicから変更しないでください。

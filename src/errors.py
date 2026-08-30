"""Application-level errors with safe user-facing messages."""


class AgentServiceError(RuntimeError):
    """Base error raised by the agent service."""

    code = "agent_error"
    user_message = "エージェントの実行中にエラーが発生しました。"

    def __init__(self) -> None:
        super().__init__(self.user_message)


class AgentTimeoutError(AgentServiceError):
    """Raised when an agent run exceeds the configured timeout."""

    code = "agent_timeout"
    user_message = "回答の生成がタイムアウトしました。時間をおいて再度お試しください。"


class AgentConnectionError(AgentServiceError):
    """Raised when Ollama cannot be reached."""

    code = "agent_connection_error"
    user_message = "Ollamaに接続できませんでした。起動状態と接続設定を確認してください。"


class AgentLimitError(AgentServiceError):
    """Raised when an agent exceeds an execution limit."""

    code = "agent_limit_exceeded"
    user_message = "ツールの実行回数が上限に達したため、処理を停止しました。"


class AgentExecutionError(AgentServiceError):
    """Raised for unexpected agent execution failures."""

    code = "agent_execution_error"
    user_message = "回答を生成できませんでした。もう一度お試しください。"


class ModelServiceError(RuntimeError):
    """Raised when Ollama model information cannot be retrieved."""

    user_message = "Ollamaのモデル情報を取得できませんでした。"

    def __init__(self) -> None:
        super().__init__(self.user_message)

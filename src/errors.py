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


class VisionServiceError(RuntimeError):
    """Base class for safe errors returned by the vision API."""

    code = "vision_model_unavailable"
    user_message = "画像解析モデルを利用できません。"
    status_code = 503

    def __init__(self) -> None:
        super().__init__(self.user_message)


class InvalidImageError(VisionServiceError):
    code = "invalid_image"
    user_message = "有効な画像ファイルを指定してください。"
    status_code = 400


class UnsupportedImageTypeError(VisionServiceError):
    code = "unsupported_image_type"
    user_message = "この画像形式は使用できません。JPEG、PNG、WebPを指定してください。"
    status_code = 400


class ImageTooLargeError(VisionServiceError):
    code = "image_too_large"
    user_message = "画像のファイルサイズまたは解像度が上限を超えています。"
    status_code = 413


class InvalidPromptError(VisionServiceError):
    code = "invalid_prompt"
    user_message = "画像解析の指示を確認してください。"
    status_code = 400


class InvalidResponseSchemaError(VisionServiceError):
    code = "invalid_response_schema"
    user_message = "レスポンス用JSON Schemaが無効または複雑すぎます。"
    status_code = 400


class VisionModelUnavailableError(VisionServiceError):
    code = "vision_model_unavailable"
    user_message = "指定された画像解析モデルを利用できません。"
    status_code = 503


class VisionTimeoutError(VisionServiceError):
    code = "vision_timeout"
    user_message = "画像解析がタイムアウトしました。時間をおいて再度お試しください。"
    status_code = 504


class SchemaValidationFailedError(VisionServiceError):
    code = "schema_validation_failed"
    user_message = "画像解析結果が指定されたJSON Schemaに適合しませんでした。"
    status_code = 502

"""Interactive Rich CLI interface for LangGraph Ollama Agent."""

import asyncio
import sys
import uuid
from langchain_core.runnables import RunnableConfig
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from src.agent import get_default_checkpointer
from src.config import get_settings
from src.errors import AgentServiceError
from src.services import AgentService
from src.tools import ALL_TOOLS, read_notes

console = Console()


def print_banner(model_name: str, base_url: str, thread_id: str, message_count: int = 0) -> None:
    """Print welcoming banner and active configuration."""
    title = "[bold magenta]🤖 LangGraph + Ollama Autonomous Agent[/bold magenta]"
    description = (
        "[dim]LangGraph ReActループとローカルOllamaモデルを連携させた自律型AIエージェント[/dim]\n\n"
        f"• [bold cyan]Model:[/bold cyan] {model_name}\n"
        f"• [bold cyan]Base URL:[/bold cyan] {base_url}\n"
        f"• [bold cyan]Thread ID:[/bold cyan] {thread_id} [dim]({message_count} 件の会話履歴)[/dim]\n"
        f"• [bold cyan]Available Tools:[/bold cyan] {', '.join(t.name for t in ALL_TOOLS)}\n\n"
        "[dim]コマンド: /reset (会話リセット), /session <ID> (セッション切替), /history (履歴確認), /notes (メモ確認), /model <名前> (モデル変更), /exit (終了)[/dim]"
    )
    console.print(Panel(description, title=title, border_style="cyan"))


async def run_cli_async() -> None:
    """Asynchronous CLI loop backed by the shared agent service."""
    settings = get_settings()
    current_model = settings.ollama_model
    # Use thread_id from settings or default
    thread_id = settings.thread_id
    checkpointer = get_default_checkpointer()

    console.print("[dim]エージェントを初期化中...[/dim]")
    try:
        agent_service = AgentService.create(
            model_name=current_model,
            checkpointer=checkpointer,
            settings=settings,
        )
    except AgentServiceError as error:
        console.print(
            f"[bold red]エージェント初期化エラー:[/bold red] {error.user_message}"
        )
        sys.exit(1)
    except Exception:
        console.print("[bold red]エージェントを初期化できませんでした。[/bold red]")
        sys.exit(1)

    # Check existing history in thread
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    try:
        state = await agent_service.aget_state(thread_id)
        msg_count = len(state.values.get("messages", [])) if state and state.values else 0
    except Exception:
        msg_count = 0

    print_banner(current_model, settings.ollama_base_url, thread_id, msg_count)

    while True:
        try:
            user_input = console.input("\n[bold green]You[/bold green] [dim]> [/dim]").strip()
            if not user_input:
                continue

            # Handle slash commands
            if user_input.lower() in ("/exit", "/quit", "exit", "quit"):
                console.print("[yellow]エージェントを終了します。お疲れ様でした！[/yellow]")
                break

            if user_input.lower() in ("/reset", "/clear"):
                thread_id = str(uuid.uuid4())
                config = {"configurable": {"thread_id": thread_id}}
                console.print(f"[cyan]会話履歴をリセットしました。(新しいThread ID: {thread_id})[/cyan]")
                continue

            if user_input.lower().startswith("/session "):
                new_sid = user_input.split(maxsplit=1)[1].strip()
                if new_sid:
                    thread_id = new_sid
                    config = {"configurable": {"thread_id": thread_id}}
                    state = await agent_service.aget_state(thread_id)
                    c = len(state.values.get("messages", [])) if state and state.values else 0
                    console.print(f"[cyan]セッションを '{thread_id}' に切り替えました。 (過去メッセージ数: {c} 件)[/cyan]")
                    continue

            if user_input.lower() == "/history":
                state = await agent_service.aget_state(thread_id)
                msgs = state.values.get("messages", []) if state and state.values else []
                if not msgs:
                    console.print("[dim]このセッションにはまだ会話履歴がありません。[/dim]")
                else:
                    console.print(f"[bold cyan]📜 会話履歴 ({len(msgs)} 件):[/bold cyan]")
                    for idx, m in enumerate(msgs, 1):
                        role = m.__class__.__name__
                        content = str(m.content)
                        if len(content) > 100:
                            content = content[:100] + "..."
                        console.print(f"  [{idx}] [bold]{role}[/bold]: {content}")
                continue

            if user_input.lower() == "/notes":
                notes_output = read_notes.invoke({}, config=config)
                console.print(Panel(notes_output, title="📝 メモ一覧", border_style="yellow"))
                continue

            if user_input.lower() == "/help":
                help_table = Table(title="利用可能なコマンド")
                help_table.add_column("コマンド", style="cyan")
                help_table.add_column("説明", style="dim")
                help_table.add_row("/reset, /clear", "新しいセッションIDを生成して会話を初期化")
                help_table.add_row("/session <ID>", "指定したセッションIDに切り替え（過去の対話を再開）")
                help_table.add_row("/history", "現在のセッションの会話履歴を表示")
                help_table.add_row("/notes", "保存されたメモを一覧表示")
                help_table.add_row("/model <name>", "使用するOllamaモデルを変更")
                help_table.add_row("/help", "このヘルプを表示")
                help_table.add_row("/exit, /quit", "アプリケーションを終了")
                console.print(help_table)
                continue

            if user_input.lower().startswith("/model "):
                new_model = user_input.split(maxsplit=1)[1].strip()
                if new_model:
                    current_model = new_model
                    agent_service = AgentService.create(
                        model_name=current_model,
                        checkpointer=checkpointer,
                        settings=settings,
                    )
                    console.print(f"[green]モデルを '{current_model}' に変更しました。（会話履歴は継続されます）[/green]")
                    continue

            with console.status("[bold blue]思考・実行中...[/bold blue]", spinner="dots"):
                final_answer = ""
                async for event in agent_service.astream_events(user_input, thread_id):
                    if event.type == "tool_started":
                        console.print(
                            f"\n[bold yellow]⚙️ ツール呼び出し:[/bold yellow] "
                            f"[bold]{event.tool_name}[/bold] (引数: {dict(event.tool_args)})"
                        )
                    elif event.type == "tool_completed":
                        content_snippet = event.content
                        if len(content_snippet) > 300:
                            content_snippet = content_snippet[:300] + "... (省略)"
                        console.print(
                            Panel(
                                content_snippet,
                                title=f"📥 ツール実行結果: {event.tool_name}",
                                border_style="dim yellow",
                            )
                        )
                    elif event.type == "assistant_completed":
                        final_answer = event.content

            if final_answer:
                console.print("\n[bold magenta]AI[/bold magenta] [dim]>[/dim]")
                console.print(Markdown(str(final_answer)))
            elif not final_answer:
                console.print("\n[dim](回答はありませんでした)[/dim]")

        except KeyboardInterrupt:
            console.print("\n[yellow]終了します。[/yellow]")
            break
        except AgentServiceError as error:
            console.print(f"\n[bold red]エラーが発生しました:[/bold red] {error.user_message}")
        except Exception:
            console.print("\n[bold red]予期しないエラーが発生しました。[/bold red]")


def run_cli() -> None:
    """Run the asynchronous CLI from a synchronous entry point."""
    asyncio.run(run_cli_async())


if __name__ == "__main__":
    run_cli()

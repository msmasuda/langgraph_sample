"""Explicit approval policy for tools with external side effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ToolRisk = Literal["read_only", "internal_write", "external_side_effect"]


class UnapprovedToolRegistrationError(RuntimeError):
    """Raised when a side-effect tool is wired without an approval executor."""


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    """Machine-readable decision made before a tool is executed."""

    tool_name: str
    risk: ToolRisk
    allowed: bool
    requires_approval: bool
    reason: str


class ToolApprovalPolicy:
    """Deny configured side-effect tools unless this execution is approved."""

    def __init__(self, approval_required_tools: frozenset[str]) -> None:
        self.approval_required_tools = approval_required_tools

    def evaluate(
        self,
        tool_name: str,
        *,
        approved: bool = False,
    ) -> ToolPolicyDecision:
        normalized = tool_name.strip()
        if normalized in self.approval_required_tools:
            return ToolPolicyDecision(
                tool_name=normalized,
                risk="external_side_effect",
                allowed=approved,
                requires_approval=not approved,
                reason=(
                    "利用者が明示的に承認しました。"
                    if approved
                    else "外部へ副作用を与えるため承認が必要です。"
                ),
            )
        risk: ToolRisk = (
            "internal_write" if normalized == "save_note" else "read_only"
        )
        return ToolPolicyDecision(
            tool_name=normalized,
            risk=risk,
            allowed=True,
            requires_approval=False,
            reason="事前承認を必要としないツールです。",
        )

    def safe_preview(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Expose argument names and value types without leaking their contents."""
        return {
            "tool_name": tool_name,
            "arguments": {
                str(name): type(value).__name__ for name, value in arguments.items()
            },
        }

    def validate_registration(self, tool_names: list[str]) -> None:
        """Fail closed until a future approval/resume executor is connected."""
        blocked = [
            name for name in tool_names if self.evaluate(name).requires_approval
        ]
        if blocked:
            names = ", ".join(sorted(blocked))
            raise UnapprovedToolRegistrationError(
                f"承認フロー未接続の副作用ツールは登録できません: {names}"
            )

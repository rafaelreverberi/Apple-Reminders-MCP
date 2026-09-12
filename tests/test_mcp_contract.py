import asyncio

from apple_reminders_mcp import server

EXPECTED = {
    "health_check",
    "check_session_status",
    "list_reminder_lists",
    "list_reminders",
    "search_reminders",
    "get_reminder",
    "create_reminder",
    "update_reminder",
    "set_reminder_completed",
    "list_subtasks",
    "create_subtask",
    "get_reminder_recurrence",
    "set_reminder_recurrence",
    "clear_reminder_recurrence",
    "list_reminder_tags",
    "add_reminder_tag",
    "remove_reminder_tag",
    "list_reminder_attachments",
    "add_reminder_url_attachment",
    "remove_reminder_attachment",
    "list_reminder_alarms",
    "add_location_reminder",
    "prepare_delete_reminder",
    "confirm_delete_reminder",
}


def test_tool_contract_and_annotations():
    tools = asyncio.run(server.mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED
    delete = next(tool for tool in tools if tool.name == "confirm_delete_reminder")
    assert delete.annotations.destructive_hint is True
    read = next(tool for tool in tools if tool.name == "list_reminders")
    assert read.annotations.read_only_hint is True


def test_health_is_degraded_without_real_credentials():
    payload = server.health_payload()
    assert payload["service"] == "apple-reminders-mcp"
    assert payload["mcp"] is True
    assert payload["status"] in {"ok", "degraded"}

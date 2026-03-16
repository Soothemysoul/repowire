import json
import tempfile
from pathlib import Path

from repowire.session.transcript import extract_last_turn_pair, extract_last_turn_tool_calls


class TestExtractLastTurnPair:
    def test_basic_pair(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "user", "message": {"content": "Hello"}}) + "\n")
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Hi there!"}]},
                    }
                )
                + "\n"
            )
            path = Path(f.name)

        user, assistant = extract_last_turn_pair(path)
        assert user == "Hello"
        assert assistant == "Hi there!"
        path.unlink()

    def test_returns_last_of_each(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "user", "message": {"content": "First question"}}) + "\n")
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "First answer"}]},
                    }
                )
                + "\n"
            )
            f.write(json.dumps({"type": "user", "message": {"content": "Second question"}}) + "\n")
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Second answer"}]},
                    }
                )
                + "\n"
            )
            path = Path(f.name)

        user, assistant = extract_last_turn_pair(path)
        assert user == "Second question"
        assert assistant == "Second answer"
        path.unlink()

    def test_no_user_messages(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Response"}]},
                    }
                )
                + "\n"
            )
            path = Path(f.name)

        user, assistant = extract_last_turn_pair(path)
        assert user is None
        assert assistant == "Response"
        path.unlink()

    def test_no_assistant_messages(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "user", "message": {"content": "Just a prompt"}}) + "\n")
            path = Path(f.name)

        user, assistant = extract_last_turn_pair(path)
        assert user == "Just a prompt"
        assert assistant is None
        path.unlink()

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = Path(f.name)

        user, assistant = extract_last_turn_pair(path)
        assert user is None
        assert assistant is None
        path.unlink()

    def test_nonexistent_file(self):
        user, assistant = extract_last_turn_pair(Path("/nonexistent/path.jsonl"))
        assert user is None
        assert assistant is None


class TestExtractToolCalls:
    def test_extracts_tool_use(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "user", "message": {"content": "Fix it"}}) + "\n")
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/auth.py"}},
                ]},
            }) + "\n")
            f.write(json.dumps({
                "type": "user",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"},
                ]},
            }) + "\n")
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text", "text": "Fixed!"},
                ]},
            }) + "\n")
            path = Path(f.name)

        calls = extract_last_turn_tool_calls(path)
        assert len(calls) == 1
        assert calls[0]["name"] == "Read"
        assert "auth.py" in calls[0]["input"]
        path.unlink()

    def test_multiple_tool_calls(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "user", "message": {"content": "Do stuff"}}) + "\n")
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                ]},
            }) + "\n")
            f.write(json.dumps({
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": ""}]},
            }) + "\n")
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/tmp/foo.py"}},
                ]},
            }) + "\n")
            f.write(json.dumps({
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": ""}]},
            }) + "\n")
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Done"}]},
            }) + "\n")
            path = Path(f.name)

        calls = extract_last_turn_tool_calls(path)
        assert len(calls) == 2
        assert calls[0]["name"] == "Bash"
        assert calls[1]["name"] == "Edit"
        path.unlink()

    def test_no_tool_calls(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "user", "message": {"content": "Hi"}}) + "\n")
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello!"}]},
            }) + "\n")
            path = Path(f.name)

        calls = extract_last_turn_tool_calls(path)
        assert calls == []
        path.unlink()

    def test_nonexistent_file(self):
        calls = extract_last_turn_tool_calls(Path("/nonexistent.jsonl"))
        assert calls == []

    def test_bash_input_summary(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"type": "user", "message": {"content": "Run it"}}) + "\n")
            f.write(json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest tests/ -v"}},
                ]},
            }) + "\n")
            path = Path(f.name)

        calls = extract_last_turn_tool_calls(path)
        assert calls[0]["input"] == "pytest tests/ -v"
        path.unlink()

"""MCP protocol error, shared by the server and the content modules.

Kept in its own module so `resources`/`prompts` can raise a JSON-RPC error
without importing the server (which imports them).
"""


class MCPError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

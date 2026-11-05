"""cairnsandbox: the Python SDK for the CairnSandbox cloud (CONTRACT §1 over HTTP, no dependencies beyond the standard library).

    from cairnsandbox import Client
    c = Client("http://127.0.0.1:18700", token="...")          # or CAIRN_URL / CAIRN_TOKEN / CAIRN_PROJECT in the environment
    sb = c.sandboxes.create(mem=512, network={"allow_hosts": ["pypi.org"]})
    print(sb.commands.run("python3 -V").stdout)
    sb.files.write("/home/agent/hello.txt", "hi")
    ck = sb.checkpoints.create("before")                          # what E2B does not have: checkpoints, rollback, fork from a point, budget, ticket, inbox
    sb.commands.run("rm /home/agent/hello.txt"); sb.checkpoints.rollback("before")
    sb.kill()

The shapes follow E2B where E2B has them (create / commands.run / files / pty / kill / set_timeout) so a user of that SDK finds their way; everything
else is ours. The server's JSON is returned as dicts; nothing is hidden behind models that lag the contract."""
from .client import Client, ApiError, NotFound, Forbidden, NoCapacity, QuotaExceeded, NeedsApproval
from .sandbox import Sandbox, CommandResult, Session

__all__ = ["Client", "Sandbox", "CommandResult", "Session", "ApiError", "NotFound", "Forbidden", "NoCapacity", "QuotaExceeded", "NeedsApproval"]
__version__ = "0.1.0"

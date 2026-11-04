# Which code host this deployment is connected to (docs/002 Connector): one of the Connector classes, picked by CODE_HOST.
from config import *
from gitea import Gitea
from github import GitHub

CONNECTORS = {"gitea": Gitea, "github": GitHub}


def make_connector(cfg):
    kind = (cfg.get("code_host") or "gitea").lower()
    if kind not in CONNECTORS: raise ValueError(f"CODE_HOST must be one of {sorted(CONNECTORS)}, not {kind!r}")
    return CONNECTORS[kind](cfg)

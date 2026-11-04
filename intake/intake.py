#!/usr/bin/env python3
"""fc-intake: the intake layer (docs/005-intake.md, docs/006-execution.md). Library + HTTP service, stdlib only.

  Source -> Trigger -> Thread -> Eval (gate + assessment) -> Decision -> Ticket + Mandate -> Assignment (one sandbox)
  -> Report (kernel ABI) -> delivery through the relay (branch + PR + comment) -> Ticket end.

Pieces (all in this file):
  Policy      allowed lists, admission table, rules, accept mode; versioned by content hash
  Sources     workbench (direct requests) and gitea (issue/PR comments, polled)
  Engine      the only writer of triggers/threads/decisions/tickets/assignments (sqlite)
  Executor    one Assignment = one sandbox over the control plane: prepare repo, run harness in ticket mode,
              read the report from the ABI event log, pack the workspace, deliver
  Relay       the only holder of the Gitea token: clone, apply the sandbox's patch, push ticket/<short>, open PR, comment
  HTTP        ThreadingHTTPServer with the /intake routes (CONTRACT §8)
Credentials never enter a sandbox; the sandbox only reaches the model proxy.
"""
import hmac, base64, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import base64, hashlib, hmac, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from config import *          # noqa: F401,F403 — the entry re-exports every module's names: scripts and tests import only this file
from policy import *         # noqa
from store import *          # noqa
from clients import *        # noqa
from gitea import *          # noqa
from github import GitHub    # noqa
from connectors import make_connector   # noqa
from sources import *        # noqa
from engine import Engine    # noqa
from engines import Engines  # noqa
from chat import ChatConnector   # noqa
from mining import classify_pr_files, MiningMixin   # noqa
from httpapi import Handler, serve   # noqa


def main(argv=None):
    cfg = cfg_from_env(); engines = Engines(cfg); e = engines.default            # the deployer's Engine owns the code host and CI triggers
    GiteaSource(e).start(); CISource(e).start()
    srv = serve(e, cfg["port"], engines)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == "__main__": sys.exit(main())

"""Cairn stage 0: the smallest harness that can do a task — one bash tool, one model, one loop.

Layout (what the self-improvement loop may edit lives in this package; the sandbox daemon, proxy and evaluator are outside):
  agent.py   the loop: system+instance prompt, query model, run actions, observe, until Submitted/limits
  model.py   OpenAI-compatible chat client (stdlib), tool-call parsing, cost from proxy headers
  env.py     local bash execution with timeouts and the COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT convention
  run.py     CLI entry: python3 -m aios.run --task-file ... --out traj.json
  config.json prompts and limits (adapted from mini-swe-agent's mini.yaml)
"""
__version__ = "0.1.0"

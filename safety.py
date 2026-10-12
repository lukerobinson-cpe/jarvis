"""Commands the local model may never run, whatever the user answers.

Every command the model proposes is already read out and needs a spoken yes
before it runs. This is the second line of defence: a small model is not
trusted with anything that destroys data, changes the system, pulls code from
the internet, or rewrites Jarvis itself, so those are refused outright.
"""
import re

FORBIDDEN = [
    (re.compile(r"\b(remove-item|rm|del|erase|rmdir|rd|ri|clear-content|clear-recyclebin|remove-itemproperty)\b", re.I), "it deletes things"),
    (re.compile(r"::\s*delete\s*\(|\.delete\s*\(|\bdeletefile\b", re.I), "it deletes things"),
    (re.compile(r"\b(format(\.com)?\s+[a-z]:|format-volume|clear-disk|diskpart|initialize-disk|remove-partition)\b", re.I), "it alters disks"),
    (re.compile(r"\b(stop-computer|restart-computer|shutdown|logoff)\b", re.I), "it shuts the computer down"),
    (re.compile(r"\breg(\.exe)?\s+(delete|add|import)\b|\b(set|new)-itemproperty\b", re.I), "it changes the registry"),
    (re.compile(r"\b(invoke-expression|iex|invoke-webrequest|iwr|curl|wget|start-bitstransfer|invoke-restmethod|irm)\b|\bnet\.webclient\b|-encodedcommand\b|\s-enc\s", re.I), "it downloads or runs things from the internet"),
    (re.compile(r"-verb\s+runas|\bset-executionpolicy\b|\buninstall|\bmsiexec\b|\bwinget\b", re.I), "it installs, removes or elevates"),
    (re.compile(r"(?<!documents)[\\/]jarvis\b|claude-memory|[\\/]\.claude\b|\.ollama", re.I), "it touches Jarvis's own files"),
]


def refusal(command: str) -> str | None:
    """Why `command` may not run, as an instruction for the model, or None if it may."""
    for pattern, reason in FORBIDDEN:
        if pattern.search(command):
            return f"Refused: that command is not allowed because {reason}. Tell {{USER}} you cannot do that and that it is one for {{USER}} to do."
    return None

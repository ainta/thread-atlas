"""Conservative, local filing for new ChatGPT conversations."""

import json
import re
import unicodedata
from itertools import islice


CHATGPT_PROJECT_PHRASES = "chatgpt_project_phrases"


def normalize(text):
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


def classify_chatgpt_project(projects, configured_phrases, title, user_prompts):
    projects = {project["id"]: project for project in projects}
    try:
        aliases = json.loads(configured_phrases)
    except (TypeError, ValueError):
        aliases = {}
    if not isinstance(aliases, dict):
        aliases = {}

    fallbacks = {}
    labels = {}
    phrases = {}
    for key, project in projects.items():
        label = normalize(project["name"])
        if label == "unclassified":
            if project["parent_id"] is not None:
                fallbacks.setdefault(project["parent_id"], []).append(key)
            continue
        rules = aliases.get(key)
        if isinstance(rules, list):
            phrases[key] = [term for phrase in rules if isinstance(phrase, str) and (term := normalize(phrase))]
        elif label:
            labels[key] = label
            if " " in label:
                phrases[key] = [label]

    def matching(text):
        bounded = f" {text} "
        return {key for key, rules in phrases.items() if any(f" {phrase} " in bounded for phrase in rules)}

    title = normalize(title)
    matches = matching(title) | {key for key, label in labels.items() if label == title}
    if not matches:
        prompts = (prompt for prompt in user_prompts if isinstance(prompt, str) and prompt.strip())
        for prompt in islice(prompts, 2):
            matches.update(matching(normalize(prompt[:800])))
    if not matches:
        return None

    ancestors = {}
    for key in matches:
        path = []
        parent = projects[key]["parent_id"]
        while parent is not None and parent in projects:
            path.append(parent)
            parent = projects[parent]["parent_id"]
        ancestors[key] = path
    deepest = matches - {ancestor for path in ancestors.values() for ancestor in path}
    if len(deepest) == 1:
        target = next(iter(deepest))
        fallback = fallbacks.get(target)
        return fallback[0] if fallback and len(fallback) == 1 else target

    roots = set.intersection(*(set(ancestors[key]) for key in deepest))
    for ancestor in ancestors[next(iter(deepest))]:
        fallback = fallbacks.get(ancestor)
        if ancestor in roots and fallback and len(fallback) == 1:
            return fallback[0]
    return None

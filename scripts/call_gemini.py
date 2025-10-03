import os
import json
import requests
import base64
import re

# --- ENVIRONMENT VARIABLES ---
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]       # e.g. "owner/repo"
PR_NUMBER = os.environ["PR_NUMBER"]          # e.g. "12"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GITHUB_EVENT_PATH = os.environ["GITHUB_EVENT_PATH"]

# --- LOAD PR EVENT TO GET BASE SHA ---
with open(GITHUB_EVENT_PATH) as f:
    event_data = json.load(f)

BASE_SHA = event_data["pull_request"]["base"]["sha"]

# --- FETCH PR DIFF IN PATCH FORMAT ---
diff_url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}"
diff_headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3.diff"
}
diff_resp = requests.get(diff_url, headers=diff_headers)
if diff_resp.status_code != 200:
    print(f"Failed to fetch PR diff: {diff_resp.status_code} {diff_resp.text}")
    exit(1)

raw_diff = diff_resp.text

# --- PARSE DIFF WITH LINE NUMBERS ---
pr_diff = {}  # { file_path: [ {type, old_line, new_line, content} ] }
current_file = None
old_line_num = new_line_num = 0

hunk_regex = re.compile(r"@@ -(\d+),?\d* \+(\d+),?\d* @@")
for line in raw_diff.splitlines():
    if line.startswith("+++ b/"):
        current_file = line.replace("+++ b/", "").strip()
        if current_file not in pr_diff:
            pr_diff[current_file] = []
    elif current_file and line.startswith("@@"):
        match = hunk_regex.match(line)
        if match:
            old_line_num = int(match.group(1))
            new_line_num = int(match.group(2))
    elif current_file:
        if line.startswith("+") and not line.startswith("+++"):
            pr_diff[current_file].append({
                "type": "added",
                "old_line": None,
                "new_line": new_line_num,
                "content": line[1:]
            })
            new_line_num += 1
        elif line.startswith("-") and not line.startswith("---"):
            pr_diff[current_file].append({
                "type": "deleted",
                "old_line": old_line_num,
                "new_line": None,
                "content": line[1:]
            })
            old_line_num += 1
        else:
            old_line_num += 1
            new_line_num += 1

# --- FETCH BASE FILE CONTENTS ---
def get_base_file_content(file_path):
    url = f"https://api.github.com/repos/{REPO}/contents/{file_path}?ref={BASE_SHA}"
    resp = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"})
    if resp.status_code != 200:
        print(f"Failed to fetch base content for {file_path}: {resp.status_code}")
        return ""
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content

# --- PROMPT TEMPLATE ---
PROMPT_TEMPLATE = """
File: {file_path}

Base code (for context):
{base_content}

Changes (with line numbers):
{diff_lines}

You are reviewing a Python/FastAPI pull request. Focus ONLY on the changes introduced.

- Type hints and return types for endpoints
- HTTP status codes and response structures
- Pydantic validation for body, query, and path parameters
- Async vs sync consistency
- Dependency injection and separation of concerns
- Error handling and logging
- Router organization and module structure

When responding:
- Be concise, professional, polite
- Provide bullet-point feedback
- Mention relevant file and approximate line numbers
- Suggest best practices or very short example snippets
- Do not include intros or meta commentary
"""

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
review_comments = []

# --- LOOP FILES & SEND TO GEMINI ---
for file_path, changes in pr_diff.items():
    if not changes:
        continue

    base_content = get_base_file_content(file_path)

    # Build diff string with inline line numbers
    diff_lines = []
    for c in changes:
        if c["type"] == "added":
            diff_lines.append(f"+ [L{c['new_line']}] {c['content']}")
        elif c["type"] == "deleted":
            diff_lines.append(f"- [L{c['old_line']}] {c['content']}")
    diff_text = "\n".join(diff_lines)

    prompt = PROMPT_TEMPLATE.format(
        file_path=file_path,
        base_content=base_content,
        diff_lines=diff_text
    )

    # Call Gemini
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY
    }
    payload = {
        "contents": [
            {"parts": [{"text": prompt}]}
        ]
    }
    resp = requests.post(GEMINI_URL, headers=headers, json=payload)
    resp_json = resp.json()
    ai_comment = (
        resp_json.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "No response from Gemini.")
    )

    review_comments.append({
        "path": file_path,
        "body": ai_comment
    })

# --- POST COMMENTS BACK TO THE PR ---
COMMENTS_URL = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"
post_headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json"
}

for comment in review_comments:
    body = f"**Review for file:** `{comment['path']}`\n\n{comment['body']}"
    response = requests.post(COMMENTS_URL, headers=post_headers, json={"body": body})
    print(response.status_code, response.text)

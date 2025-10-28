import os
import json
import requests
import base64
import re

# --- ENVIRONMENT VARIABLES ---
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
PR_NUMBER = os.environ["PR_NUMBER"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GITHUB_EVENT_PATH = os.environ["GITHUB_EVENT_PATH"]
BASE_COMMIT_SHA = os.getenv("BASE_SHA")
HEAD_COMMIT_SHA = os.getenv("GITHUB_SHA")


# --- LOAD PR EVENT TO GET BASE SHA ---
with open(GITHUB_EVENT_PATH) as f:
    event_data = json.load(f)

BASE_SHA = event_data["pull_request"]["base"]["sha"]

# --- FETCH DIFF FOR SPECIFIC COMMIT (OR FULL PR IF FIRST RUN) ---
diff_headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3.diff"
}

if BASE_COMMIT_SHA:
    # Compare only between the last and current commit
    diff_url = f"https://api.github.com/repos/{REPO}/compare/{BASE_COMMIT_SHA}...{HEAD_COMMIT_SHA}"
    print(f"Fetching diff between commits: {BASE_COMMIT_SHA[:7]}...{HEAD_COMMIT_SHA[:7]}")
else:
    # Fallback: full PR diff if BASE_SHA not provided
    diff_url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}"
    print("BASE_SHA not found — reviewing full PR diff.")

diff_resp = requests.get(diff_url, headers=diff_headers)
if diff_resp.status_code != 200:
    print(f"Failed to fetch diff: {diff_resp.status_code} {diff_resp.text}")
    exit(1)

raw_diff = diff_resp.text

if not raw_diff.strip():
    print("No diff detected between commits — skipping review.")
    exit(0)

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

Changes (changes made in this pull request with line numbers):
{diff_lines}

You are a senior engineer reviewing a pull request. Focus ONLY on the changes introduced in this diff.

### What to Look For when reviewing

Evaluate the changes against these areas:

**FastAPI / Backend Concerns**
- Type hints and return types for endpoints
- HTTP status codes and response structure
- Input validation with Pydantic (body, query, path)
- Async vs sync consistency
- Dependency injection patterns and separation of concerns
- Error handling, logging, and exceptions
- Router structure and module organization

**General Code Quality**
- Logic errors or potential bugs
- Edge cases not handled
- Missing checks for None or invalid inputs
- Clarity, readability, and maintainability

**Performance & Efficiency**
- Inefficient operations or redundant calls
- Database query usage and resource handling
- Blocking I/O in async contexts

**Security**
- Input sanitization
- Injection risks
- Authentication/Authorization concerns
- Exposure of sensitive information

**Architecture & Patterns**
- Reusability and modularity
- Proper separation of concerns

### Output Rules

Respond with:
- Bullet points only
- Include the relevant file and approximate line numbers for each point
- Use severity levels: CRITICAL, HIGH, MEDIUM, LOW
- Include short suggestions or mini code snippets only when needed
- No intros, pleasantries, or meta commentary — start directly with actionable feedback
- Keep the tone direct, first-person, professional, and polite
- Be concise and focus strictly on the changes in this pull request

### Output Structure

Follow this format:

**Issues Found:**
- [SEVERITY] Issue description (file + location)
- [SEVERITY] Issue description (file + location)

**Suggestions:**
- Specific improvements or alternatives
- Short example snippets when relevant

**Positive Notes:**
- Well-implemented aspects (if any)

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

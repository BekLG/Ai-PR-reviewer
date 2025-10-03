import os
import requests
import re

# --- ENVIRONMENT VARIABLES ---
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]     # e.g. "owner/repo"
PR_NUMBER = os.environ["PR_NUMBER"]        # e.g. "12"
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# --- GITHUB API: Fetch PR diff in patch format ---
diff_url = f"https://api.github.com/repos/{REPO}/pulls/{PR_NUMBER}"
headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3.diff"
}
diff_response = requests.get(diff_url, headers=headers)

if diff_response.status_code != 200:
    print(f"Failed to fetch PR diff: {diff_response.status_code} {diff_response.text}")
    exit(1)

raw_diff = diff_response.text

# --- IMPROVED PARSE OF DIFF ---
pr_diff = {}  # { "file_path": ["changed line1", "changed line2", ...] }
current_file = None

for line in raw_diff.splitlines():
    # Detect a file from "+++ b/<path>" which is more reliable than parsing "diff --git"
    if line.startswith("+++ b/"):
        current_file = line.replace("+++ b/", "").strip()
        if current_file and current_file not in pr_diff:
            pr_diff[current_file] = []
    elif current_file and line.startswith("+") and not line.startswith("+++"):
        pr_diff[current_file].append(line[1:])

# --- PROMPT TEMPLATE ---
PROMPT_TEMPLATE = """
You are reviewing a Python/FastAPI pull request. Focus only on the changes introduced in this diff:

{code}

When reviewing, pay close attention to:

- Type hints and return types for endpoints
- HTTP status codes and response structures
- Pydantic validation for body, query, and path parameters
- Async vs sync consistency
- Dependency injection and separation of concerns
- Error handling and logging
 Router organization and module structure

When responding:

- Be concise and get to the point
- Use a polite, professional first-person tone
- Provide bullet-point feedback
- Mention the relevant file and approximate location for each issue
- Suggest best practices or very short example snippets when needed
- Do not include any intros or meta commentary—start directly with feedback

"""

# --- GEMINI SETTINGS ---
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
review_comments = []

# --- LOOP FILES & SEND TO GEMINI ---
for file_path, changes in pr_diff.items():
    combined_snippet = "\n".join(changes).strip()
    if not combined_snippet:
        continue

    prompt = PROMPT_TEMPLATE.format(code=combined_snippet)

    g_headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY
    }

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }

    resp = requests.post(GEMINI_URL, headers=g_headers, json=payload)
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

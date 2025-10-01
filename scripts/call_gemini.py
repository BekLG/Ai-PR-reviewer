import os
import sys
import json
import requests

diff_file = sys.argv[1]

# Load diff JSON
with open(diff_file) as f:
    pr_diff = json.load(f)

# Custom prompt template
PROMPT_TEMPLATE = """
You are an expert code reviewer.
For the following code snippet, provide concise, constructive comments.
Code:
{code}
"""

# Gemini API endpoint
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

review_comments = []

for file_path, changes in pr_diff.items():
    # Combine all changed lines in this file
    combined_snippet = "\n".join([c["code"] for c in changes])
    prompt = PROMPT_TEMPLATE.format(code=combined_snippet)

    headers = {
    "Content-Type": "application/json",
    "X-goog-api-key": os.environ["GEMINI_API_KEY"]
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

    resp = requests.post(GEMINI_URL, headers=headers, json=payload)
    resp_json = resp.json()

    ai_comment = (
        resp_json.get("candidates", [{}])[0]
          .get("content", {})
          .get("parts", [{}])[0]
          .get("text", "No response from Gemini.")
    )

    # Save a single comment per file
    review_comments.append({
        "path": file_path,
        "body": ai_comment
    })

# Post to GitHub ISSUE COMMENTS endpoint (one comment per file)
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
PR_NUMBER = os.environ["PR_NUMBER"]

GITHUB_API_URL = f"https://api.github.com/repos/{REPO}/issues/{PR_NUMBER}/comments"

headers = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "Content-Type": "application/json"
}

for comment in review_comments:
    body = f"**Review for file:** `{comment['path']}`\n\n{comment['body']}"
    resp = requests.post(GITHUB_API_URL, headers=headers, json={"body": body})
    print(resp.status_code, resp.text)

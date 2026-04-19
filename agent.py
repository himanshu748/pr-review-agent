import os
import json
from dotenv import load_dotenv
from huggingface_hub import AsyncInferenceClient

# Loads HF_TOKEN from .env using python-dotenv
load_dotenv()

async def review_pr(pr_data: dict) -> dict:
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token or hf_token == "your_huggingface_token_here":
        return {"error": "HF_TOKEN is not set in .env"}
        
    client = AsyncInferenceClient(token=hf_token)
    model = "Qwen/Qwen2.5-72B-Instruct"
    
    system_prompt = "You are a senior software engineer doing a thorough PR review."
    
    user_prompt = f"""Review the following Pull Request and return ONLY a JSON object.

Title: {pr_data.get('title')}
Author: {pr_data.get('author')}
Description: {pr_data.get('description')}

Stats:
- Commits: {pr_data.get('commits')}
- Changed files: {pr_data.get('changed_files')}
- Additions: {pr_data.get('additions')}
- Deletions: {pr_data.get('deletions')}

Diff:
{pr_data.get('diff')}

You must return ONLY a JSON object with the following keys:
- summary (string): what this PR does
- issues (list of objects with keys: severity (string: "high/medium/low"), file (string), comment (string))
- suggestions (list of strings)
- verdict (string): "APPROVE" or "REQUEST CHANGES"
- verdict_reason (string): one sentence why

Do not include any markdown formatting like ```json or any other text before or after the JSON.
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    try:
        response = await client.chat_completion(
            model=model,
            messages=messages,
            max_tokens=2048,
        )
        content = response.choices[0].message.content.strip()
        
        # Strip potential markdown formatting
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
            
        if content.endswith("```"):
            content = content[:-3]
            
        content = content.strip()
        
        review_result = json.loads(content)
        return review_result
    except json.JSONDecodeError as e:
        return {"error": "Failed to parse JSON response from LLM.", "raw_content": content}
    except Exception as e:
        return {"error": f"LLM error: {str(e)}"}

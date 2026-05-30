from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.llm.gemini_runtime import invoke_openai_messages

key = "EMPTY"
model = "gemini-2.5-flash"
url = ""

def extract_answer(question, output, prompt, model_name=model):
    try:
        response = invoke_openai_messages(
            [
                {
                    "role": "user",
                    "content": prompt,
                },
                {
                    "role": "assistant",
                    "content": "\n\nQuestion:{}\nAnalysis:{}\n".format(question, output)
                }
            ]
        )
    except:
        response = "Failed"
    
    return response
from pathlib import Path
import os
import json
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from dotenv import load_dotenv
load_dotenv()

PROMPTS_DIR = Path('prompts')

def load_prompt(name):
    return (PROMPTS_DIR / name).read_text()

llm = ChatOpenAI(
    base_url=os.getenv('PROVIDER_BASE_URL'),
    api_key=os.getenv('PROVIDER_API_KEY'),
    max_tokens=8192,
)

prompt = load_prompt('node1_analyzer.txt')
orig = Path('original_example.py').read_text()
mig = Path('migrated_example.py').read_text()
prompt = prompt.replace('{original_code}', orig).replace('{migrated_code}', mig)
resp = llm.invoke([HumanMessage(content=prompt)])
print('---RAW RESPONSE---')
print(resp.content)

try:
    print('\n---PARSED JSON---')
    print(json.loads(resp.content))
except Exception as e:
    print('\n---JSON PARSE ERROR---')
    print(e)

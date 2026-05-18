from typing import TypedDict, List, Dict
from langchain_groq import ChatGroq
import os
from langgraph.graph import StateGraph, END
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


class CodeReviewRequest(BaseModel):
    code: str


class CodeReviewState(TypedDict):
    """Represents the state of a code review process."""
    code: str
    initial_analysis: str
    issues: List[str]
    final_report: str

class SimpleCodeReviewAgent:
    def __init__(self, model_name: str):
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("A variável de ambiente GROQ_API_KEY não foi encontrada no sistema!")

        self.llm = ChatGroq(
            api_key=api_key,
            model_name="llama-3.3-70b-versatile",
            temperature=0.0
        )
        self.graph = self._build_graph()

    def _analysis_agent(self, state: CodeReviewState) -> Dict:
        """Step1: Analyse the code"""
        prompt = f"""Analyse the code briefly:
            {state['code']}
        Focus on: purpose, structure and concerns.
        """
        response = self.llm.invoke(prompt)
        return {"initial_analysis": response.content}
    
    def _find_issues(self, state: CodeReviewState) -> Dict:
        """Step2 : Find the issues in code"""
        prompt = f"""Based on:{state["initial_analysis"]}
        Code: {state['code']}

        List 3-5 specific issues. Format each as "-issue".
        """
        
        response = self.llm.invoke(prompt)
        issues = [line.strip() for line in response.content.split('\n') if line.strip().startswith('-')]

        return {"issues": issues}

    def _generate_report(self, state: CodeReviewState) -> Dict:
        """Step3: Generate report from the review"""
        # Garante que issues seja tratado como lista mesmo se vier vazio
        issues_text = '\n'.join(state.get('issues', []))

        prompt = f"""Create a code review report:
        
        Analysis: {state['initial_analysis']}
        Issues: {issues_text}

        Format Summary, Issues, and Recommendation.
        """
        
        response = self.llm.invoke(prompt)

        return {"final_report": response.content}

    def _build_graph(self) -> StateGraph:
        """Build the langgraph workflow"""

        workflow = StateGraph(CodeReviewState)

        # Add nodes 
        workflow.add_node("analyzer", self._analysis_agent)
        workflow.add_node("issue_finder", self._find_issues)
        workflow.add_node("report_generator", self._generate_report)

        # Add edges 
        workflow.set_entry_point("analyzer")
        workflow.add_edge("analyzer", "issue_finder")
        workflow.add_edge("issue_finder", "report_generator")
        workflow.add_edge("report_generator", END)

        return workflow.compile()
    
agent = SimpleCodeReviewAgent(model_name="llama-3.1-70b-versatile")
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/review")
def review_code(request: CodeReviewRequest):

    initial_state = {
        "code": request.code,
        "initial_analysis": "",
        "issues": [], 
        "final_report": ""
    }

    result = agent.graph.invoke(initial_state)

    return {
        "analysis": result.get('initial_analysis', ''),
        "issues": result.get("issues", []),
        "report": result.get("final_report", "")
    }
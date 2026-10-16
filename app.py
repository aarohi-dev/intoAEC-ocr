"""
Root application launcher for intoAEC-ocr web workspace.
Exports the FastAPI app instance from boqparser.api.app.
"""

from boqparser.api.app import app

if __name__ == "__main__":
    import uvicorn
    print("Starting intoAEC-ocr BOQ Parser AI Web Interface on http://localhost:8000 ...")
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)

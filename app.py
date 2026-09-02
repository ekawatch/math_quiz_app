import os
from pathlib import Path
import json
import time
import random
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# กำหนด Path ให้ชี้ไปที่โฟลเดอร์ templates อย่างถูกต้อง
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="MathQuiz Live & Self-Paced Web App")

# ==================== In-Memory State ====================
class QuizState:
    def __init__(self):
        self.raw_quiz: Optional[Dict[str, Any]] = None
        self.is_live: bool = False
        self.live_duration_seconds: int = 0
        self.start_timestamp: float = 0.0
        self.live_shuffled_questions: List[Dict[str, Any]] = []
        self.submissions: List[Dict[str, Any]] = []
        self.connected_teachers: List[WebSocket] = []

    def set_quiz(self, quiz_data: Dict[str, Any]):
        self.raw_quiz = quiz_data
        self.is_live = False
        self.submissions = []
        self.live_shuffled_questions = []

    def start_live(self, duration_minutes: int):
        if not self.raw_quiz or "questions" not in self.raw_quiz:
            return False
        self.is_live = True
        self.live_duration_seconds = duration_minutes * 60
        self.start_timestamp = time.time()
        self.submissions = []
        
        # สุ่มลำดับข้อสอบ 1 ชุด เพื่อใช้ร่วมกันสำหรับโหมด Live
        shuffled = list(self.raw_quiz["questions"])
        random.shuffle(shuffled)
        self.live_shuffled_questions = shuffled
        return True

    def stop_live(self):
        self.is_live = False
        self.start_timestamp = 0.0

    def get_remaining_seconds(self) -> int:
        if not self.is_live:
            return 0
        elapsed = time.time() - self.start_timestamp
        remaining = int(self.live_duration_seconds - elapsed)
        if remaining <= 0:
            self.stop_live()
            return 0
        return remaining

    def add_submission(self, student_name: str, answers: Dict[str, int]):
        if not self.raw_quiz:
            return
        
        questions = self.raw_quiz["questions"]
        q_map = {str(q["id"]): q for q in questions}
        
        score = 0
        details = {}
        for q_id_str, chosen_idx in answers.items():
            if q_id_str in q_map:
                correct_idx = q_map[q_id_str]["correct_answer_index"]
                is_correct = (chosen_idx == correct_idx)
                if is_correct:
                    score += 1
                details[q_id_str] = {
                    "chosen": chosen_idx,
                    "correct": correct_idx,
                    "is_correct": is_correct
                }
        
        self.submissions.append({
            "student_name": student_name,
            "score": score,
            "total": len(questions),
            "answers": details,
            "submitted_at": time.time()
        })

    def get_analytics(self) -> Dict[str, Any]:
        if not self.raw_quiz or not self.submissions:
            total_q = len(self.raw_quiz["questions"]) if self.raw_quiz else 10
            return {
                "total_students": len(self.submissions),
                "histogram": {"labels": [str(i) for i in range(total_q + 1)], "data": [0]*(total_q + 1)},
                "error_ranking": []
            }
        
        total_q = len(self.raw_quiz["questions"])
        scores = [sub["score"] for sub in self.submissions]
        
        # คำนวณ Histogram
        hist_data = [0] * (total_q + 1)
        for s in scores:
            if 0 <= s <= total_q:
                hist_data[s] += 1
                
        # วิเคราะห์ข้อที่ตอบผิด (Item Analysis)
        q_stats = {}
        for q in self.raw_quiz["questions"]:
            q_id = str(q["id"])
            q_stats[q_id] = {
                "id": q["id"],
                "question": q["question"],
                "choices": q["choices"],
                "correct_answer_index": q["correct_answer_index"],
                "wrong_count": 0,
                "total_answered": 0,
                "choice_counts": [0, 0, 0, 0]
            }

        for sub in self.submissions:
            for q_id_str, ans in sub["answers"].items():
                if q_id_str in q_stats:
                    q_stats[q_id_str]["total_answered"] += 1
                    chosen = ans["chosen"]
                    if 0 <= chosen < 4:
                        q_stats[q_id_str]["choice_counts"][chosen] += 1
                    if not ans["is_correct"]:
                        q_stats[q_id_str]["wrong_count"] += 1

        error_ranking = []
        for q_id, data in q_stats.items():
            err_rate = (data["wrong_count"] / data["total_answered"] * 100) if data["total_answered"] > 0 else 0
            error_ranking.append({
                "id": data["id"],
                "question": data["question"],
                "choices": data["choices"],
                "correct_answer_index": data["correct_answer_index"],
                "wrong_count": data["wrong_count"],
                "total_answered": data["total_answered"],
                "error_rate": round(err_rate, 1),
                "choice_counts": data["choice_counts"]
            })

        # เรียงจากข้อที่คนตอบผิดมากที่สุด ไปหาน้อยที่สุด
        error_ranking.sort(key=lambda x: x["error_rate"], reverse=True)

        return {
            "total_students": len(self.submissions),
            "histogram": {
                "labels": [f"{i} คะแนน" for i in range(total_q + 1)],
                "data": hist_data
            },
            "error_ranking": error_ranking
        }

state = QuizState()

# Default Sample Quiz
sample_quiz = {
    "quiz_title": "แบบทดสอบวิชาคณิตศาสตร์: แคลคูลัสและพีชคณิต",
    "time_limit_minutes": 10,
    "questions": [
        {
            "id": 1,
            "question": "จงหาค่าของ $\\lim_{x \\to 0} \\frac{\\sin(2x)}{x}$",
            "choices": ["0", "1", "2", "หาค่าไม่ได้"],
            "correct_answer_index": 2,
            "explanation": "เนื่องจาก $\\lim_{x \\to 0} \\frac{\\sin(kx)}{x} = k$ ดังนั้น $\\lim_{x \\to 0} \\frac{\\sin(2x)}{x} = 2$"
        },
        {
            "id": 2,
            "question": "กำหนดให้ $f(x) = x^3 - 3x^2 + 5$ จงหาค่าของ $f'(2)$",
            "choices": ["$0$", "$3$", "$-1$", "$12$"],
            "correct_answer_index": 0,
            "explanation": "ดิฟ $f(x)$ ได้ $f'(x) = 3x^2 - 6x$ แทนค่า $x = 2$ ได้ $f'(2) = 3(4) - 6(2) = 12 - 12 = 0$"
        }
    ]
}
state.set_quiz(sample_quiz)

# ==================== Routes ====================
# ปรับปรุง TemplateResponse ให้เป็นรูปแบบใหม่ของ Starlette/FastAPI
@app.get("/", response_class=HTMLResponse)
async def student_page(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="student.html"
    )

@app.get("/teacher", response_class=HTMLResponse)
async def teacher_page(request: Request):
    return templates.TemplateResponse(
        request=request, 
        name="teacher.html"
    )

@app.get("/api/template")
async def get_template():
    template_data = {
        "quiz_title": "ใส่ชื่อชุดข้อสอบที่นี่",
        "time_limit_minutes": 15,
        "questions": [
            {
                "id": 1,
                "question": "โจทย์ข้อที่ 1 ใส่สูตรคณิตศาสตร์ด้วยรูปแบบ LaTeX เช่น $\\sqrt{x^2 + y^2}$",
                "choices": ["ตัวเลือก 1", "ตัวเลือก 2", "ตัวเลือก 3", "ตัวเลือก 4"],
                "correct_answer_index": 0,
                "explanation": "อธิบายเหตุผลว่าทำไมตัวเลือกที่ 1 ถูก และตัวเลือกอื่นผิด พร้อมสูตร $\\int f(x) dx$"
            }
        ]
    }
    return JSONResponse(
        content=template_data,
        headers={"Content-Disposition": "attachment; filename=quiz_template.json"}
    )

@app.post("/api/upload")
async def upload_quiz(file: UploadFile = File(...)):
    try:
        content = await file.read()
        quiz_data = json.loads(content.decode("utf-8"))
        state.set_quiz(quiz_data)
        await notify_teachers({"type": "QUIZ_UPDATED", "quiz": quiz_data})
        return {"status": "success", "message": "อัปโหลดชุดโจทย์เรียบร้อยแล้ว", "title": quiz_data.get("quiz_title")}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

@app.get("/api/quiz-status")
async def quiz_status():
    remaining = state.get_remaining_seconds()
    return {
        "is_live": state.is_live,
        "remaining_seconds": remaining,
        "quiz_title": state.raw_quiz.get("quiz_title") if state.raw_quiz else "ไม่มีชุดข้อสอบ",
        "has_quiz": state.raw_quiz is not None
    }

@app.get("/api/get-quiz")
async def get_quiz(mode: str = "self", order: str = "sequential"):
    if not state.raw_quiz:
        return JSONResponse(status_code=404, content={"message": "ยังไม่ได้อัปโหลดข้อสอบ"})
    
    if state.is_live:
        clean_questions = []
        for q in state.live_shuffled_questions:
            clean_questions.append({
                "id": q["id"],
                "question": q["question"],
                "choices": q["choices"]
            })
        return {
            "mode": "live",
            "quiz_title": state.raw_quiz["quiz_title"],
            "time_limit": state.get_remaining_seconds(),
            "questions": clean_questions
        }
    
    questions = list(state.raw_quiz["questions"])
    if order == "random":
        random.shuffle(questions)
        
    return {
        "mode": "self",
        "quiz_title": state.raw_quiz["quiz_title"],
        "questions": questions
    }

@app.post("/api/start-timer")
async def start_timer(request: Request):
    data = await request.json()
    duration = int(data.get("duration", state.raw_quiz.get("time_limit_minutes", 10) if state.raw_quiz else 10))
    if state.start_live(duration):
        await notify_teachers({"type": "SESSION_STARTED", "duration": duration * 60})
        return {"status": "success", "duration_seconds": duration * 60}
    return JSONResponse(status_code=400, content={"status": "error", "message": "ไม่พบชุดข้อสอบ"})

@app.post("/api/stop-timer")
async def stop_timer():
    state.stop_live()
    await notify_teachers({"type": "SESSION_STOPPED"})
    return {"status": "success"}

@app.post("/api/submit")
async def submit_quiz(request: Request):
    data = await request.json()
    name = data.get("student_name", "Anonymous")
    answers = data.get("answers", {})
    state.add_submission(name, answers)
    
    analytics = state.get_analytics()
    await notify_teachers({"type": "NEW_SUBMISSION", "analytics": analytics})
    return {"status": "success"}

# ==================== WebSocket ====================
@app.websocket("/ws/teacher")
async def ws_teacher_endpoint(websocket: WebSocket):
    await websocket.accept()
    state.connected_teachers.append(websocket)
    try:
        analytics = state.get_analytics()
        init_data = {
            "type": "INIT",
            "is_live": state.is_live,
            "remaining_seconds": state.get_remaining_seconds(),
            "quiz": state.raw_quiz,
            "analytics": analytics
        }
        await websocket.send_text(json.dumps(init_data))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        state.connected_teachers.remove(websocket)

async def notify_teachers(message: dict):
    disconnected = []
    text_data = json.dumps(message)
    for ws in state.connected_teachers:
        try:
            await ws.send_text(text_data)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        state.connected_teachers.remove(ws)

# ==================== Main Runner ====================
if __name__ == "__main__":
    import uvicorn
    # ดึงพอร์ตอัตโนมัติจาก Render Environment Variable
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
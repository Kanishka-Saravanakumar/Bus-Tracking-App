import math
import time
import asyncio
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, Boolean, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from pydantic import BaseModel

# --- 1. Database & Security Configuration ---
# Success Metric: Zero plain-text passwords (using bcrypt)
SQLALCHEMY_DATABASE_URL = "sqlite:///./college_bus_master.db" 
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI()

# --- 2. Performance Middleware (Success Metric: Speed) ---
@app.middleware("http")
async def monitor_performance(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    # Log if redirection/auth exceeds the 1.5s metric
    if duration > 1.5:
        print(f"PERFORMANCE ALERT: {request.url.path} took {duration:.2f}s")
    response.headers["X-Response-Time"] = str(duration)
    return response

# --- 3. Database Schema (Success Metric: Data Integrity) ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(120), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20)) # Student, Driver, Teacher
    stop_lat = Column(Float, nullable=True)
    stop_lng = Column(Float, nullable=True)
    boarding_spot_name = Column(String(100), nullable=True)

class BusRoute(Base):
    __tablename__ = "bus_routes"
    bus_id = Column(Integer, primary_key=True)
    driver_id = Column(Integer, ForeignKey("users.id"))
    current_lat = Column(Float, default=0.0)
    current_long = Column(Float, default=0.0)
    status = Column(String(100), default="On Time")

class Attendance(Base):
    __tablename__ = "attendance"
    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("users.id"), unique=True)
    bus_id = Column(Integer, ForeignKey("bus_routes.bus_id"))
    timestamp = Column(DateTime, default=datetime.utcnow)
    is_present = Column(Boolean, default=False)

Base.metadata.create_all(bind=engine)

# --- 4. Logic & Real-Time Connection Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, WebSocket] = {}

    async def connect(self, user_id: int, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int):
        self.active_connections.pop(user_id, None)

    async def broadcast(self, message: dict):
        for connection in self.active_connections.values():
            await connection.send_json(message)

    async def send_to_user(self, user_id: int, message: dict):
        if user_id in self.active_connections:
            await self.active_connections[user_id].send_json(message)

manager = ConnectionManager()

def haversine(lat1, lon1, lat2, lon2):
    """F4: Math for Smart ETA and Alerts"""
    r = 6371.0 
    p1, l1, p2, l2 = map(math.radians, [lat1, lon1, lat2, lon2])
    a = math.sin((p2-p1)/2)**2 + math.cos(p1)*math.cos(p2)*math.sin((l2-l1)/2)**2
    return 2 * r * math.asin(math.sqrt(a))

# --- 5. API Endpoints (Auth, Boarding Change, Logic) ---

class BoardingChange(BaseModel):
    student_id: int
    new_spot: str
    lat: float
    lng: float

@app.post("/login")
async def login(email: str, password: str):
    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()
    if not user or not pwd_context.verify(password, user.password_hash):
        db.close()
        raise HTTPException(status_code=401, detail="Invalid Credentials")
    
    dashboards = {"Student": "/student/map", "Driver": "/driver/dash", "Teacher": "/teacher/view"}
    db.close()
    return {"status": "success", "redirect": dashboards.get(user.role)}

@app.post("/student/update-boarding")
async def update_boarding(req: BoardingChange):
    db = SessionLocal()
    student = db.query(User).filter(User.id == req.student_id).first()
    if student:
        student.boarding_spot_name = req.new_spot
        student.stop_lat, student.stop_lng = req.lat, req.lng
        db.commit()
        # F5: Instant Teacher Alert
        await manager.broadcast({
            "type": "NEW_NOTIFICATION",
            "msg": f"Student {student.email} changed spot to {req.new_spot}"
        })
    db.close()
    return {"status": "updated"}

# --- 6. The WebSocket Engine (Live Tracking & Proximity) ---

async def proximity_engine(bus_id: int, bus_lat: float, bus_lng: float):
    db = SessionLocal()
    # Check students within 1.0km
    students = db.query(User).filter(User.role == "Student", User.stop_lat.isnot(None)).all()
    for s in students:
        if haversine(bus_lat, bus_lng, s.stop_lat, s.stop_lng) < 1.0:
            await manager.send_to_user(s.id, {"type": "PROXIMITY_ALERT", "msg": "Bus is within 1km!"})
    db.close()

@app.websocket("/ws/live/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int, bg_tasks: BackgroundTasks):
    await manager.connect(user_id, websocket)
    db = SessionLocal()
    try:
        while True:
            data = await websocket.receive_json()
            
            # F1 & F2: Driver Update
            if data.get("type") == "GPS_UPDATE":
                bus = db.query(BusRoute).filter(BusRoute.driver_id == user_id).first()
                if bus:
                    bus.current_lat, bus.current_long = data["lat"], data["lng"]
                    db.commit()
                    await manager.broadcast({"type": "BUS_MOVE", "lat": bus.current_lat, "lng": bus.current_long})
                    bg_tasks.add_task(proximity_engine, bus.bus_id, bus.current_lat, bus.current_long)

            # F3: Attendance Integrity
            if data.get("type") == "MARK_BOARDED":
                student_id = data.get("student_id")
                attendance = db.query(Attendance).filter_by(student_id=student_id).first()
                if not attendance:
                    attendance = Attendance(student_id=student_id, is_present=True)
                    db.add(attendance)
                else:
                    attendance.is_present = True
                db.commit()
                # Notify Teacher View
                await manager.broadcast({"type": "ATTENDANCE_SYNC", "student_id": student_id})

    except WebSocketDisconnect:
        manager.disconnect(user_id)
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
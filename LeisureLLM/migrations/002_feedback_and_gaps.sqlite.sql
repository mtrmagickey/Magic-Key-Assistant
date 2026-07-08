-- Migration: Feedback tracking and knowledge gap detection (SQLite)
-- Date: 2025-12-20
-- Purpose: Support learning loop with user feedback and gap-driven interviews

-- Track user feedback on bot responses
CREATE TABLE IF NOT EXISTS response_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    feedback TEXT NOT NULL CHECK (feedback IN ('helpful', 'not_helpful')),
    channel_id INTEGER,
    message_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    
    -- Index for analytics
    improvement_memo_created INTEGER DEFAULT 0,  -- Boolean
    memo_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_feedback_created ON response_feedback(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_negative ON response_feedback(feedback) WHERE feedback = 'not_helpful';
CREATE INDEX IF NOT EXISTS idx_feedback_user ON response_feedback(user_id);

-- Track knowledge gaps the bot encounters
CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT,
    
    -- Tracking
    first_asked TEXT DEFAULT (datetime('now')),
    last_asked TEXT DEFAULT (datetime('now')),
    times_asked INTEGER DEFAULT 1,
    asked_by_users TEXT DEFAULT '[]',  -- JSON array of user IDs as text
    
    -- Status
    status TEXT DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'resolved')),
    resolved_at TEXT,
    resolved_via TEXT,  -- 'interview', 'manual_memo', 'ingested_doc'
    
    -- Metadata
    priority_score INTEGER DEFAULT 0,  -- Calculated from times_asked, recency, user roles
    assigned_to_user INTEGER,  -- Which partner should answer this
    memo_path TEXT,  -- Path to generated memo if resolved
    notes TEXT,  -- Skip reasons and other notes appended during interviews
    
    UNIQUE(topic, question)
);

CREATE INDEX IF NOT EXISTS idx_gaps_status ON knowledge_gaps(status);
CREATE INDEX IF NOT EXISTS idx_gaps_priority ON knowledge_gaps(priority_score DESC, last_asked DESC);
CREATE INDEX IF NOT EXISTS idx_gaps_topic ON knowledge_gaps(topic);

-- Track interview sessions
CREATE TABLE IF NOT EXISTS interview_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interviewer_user_id INTEGER NOT NULL,
    interviewer_username TEXT,
    channel_id INTEGER NOT NULL,
    thread_id INTEGER,  -- Discord thread for the interview
    
    started_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT,
    
    questions_asked INTEGER DEFAULT 0,
    questions_answered INTEGER DEFAULT 0,
    memos_created INTEGER DEFAULT 0,
    
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'completed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_interview_user ON interview_sessions(interviewer_user_id);
CREATE INDEX IF NOT EXISTS idx_interview_status ON interview_sessions(status);

-- Link gaps to interview questions
CREATE TABLE IF NOT EXISTS interview_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES interview_sessions(id) ON DELETE CASCADE,
    gap_id INTEGER REFERENCES knowledge_gaps(id) ON DELETE SET NULL,
    
    question TEXT NOT NULL,
    answer TEXT,
    answered_at TEXT,
    
    memo_generated INTEGER DEFAULT 0,  -- Boolean
    memo_path TEXT,
    
    order_index INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interview_questions_session ON interview_questions(session_id);

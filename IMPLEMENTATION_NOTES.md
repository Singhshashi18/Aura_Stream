# Aura-Stream Interruption & Resumption Features

## Features Implemented

### 1. Background Noise Filtering (User vs Doorbell Detection)

**Problem**: Without this feature, any loud noise (doorbell, alert, music) would trigger the assistant's interruption logic, causing unnecessary response cancellations.

**Solution**: Smart RMS variance detection
- **How it works**:
  - Tracks RMS energy values over the last 10 audio chunks
  - Calculates variance of RMS values (measures how much energy is changing)
  - High variance + high energy = User speech (modulating energy)
  - Low variance + steady tone = Background noise like doorbell (constant energy)
  - Silence = Below RMS threshold (420.0)

**Benefits**:
- Doorbell rings → Ignored, assistant continues
- User speaks → Response cancelled, awaits new input
- Vehicle honk → Ignored (too steady/constant)
- User mumbling → Detected, response cancelled

**Detection Types** (logged in debug panel):
- `"user_speech"` - Actual user talking, causes interruption
- `"background_noise"` - Steady noise like doorbell, ignored
- `"silence"` - Below threshold, no action

**Configuration**:
- RMS threshold: 420.0 (energy level for detection)
- RMS variance threshold: 5000 (for speech)
- Energy gradient min: 50
- History window: 10 chunks (320ms at 16kHz, 20ms/chunk)

---

### 2. Response Resumption with Context Pivoting

**Problem**: When interrupted mid-response, the assistant would just start fresh on the user's new request, losing context of what it was about to say.

**Solution**: Track interrupted responses and inject resumption context

**How it works**:
1. When `input_audio_buffer.speech_started` event arrives during response:
   - Capture partial response text (what assistant was saying)
   - Set `was_recently_interrupted = True` flag
   - Store in `interrupted_response` field
2. When building next response's instructions:
   - Check if `was_recently_interrupted` flag is set
   - Inject resumption context into system prompt
   - Tell assistant: "I was saying X, but you interrupted with Y. Let me pivot..."
3. When response completes, store in database:
   - `interrupted_by`: The partial response that was interrupted
   - `interruption_type`: "user_speech" (only for real interruptions)
   - `resumption_context`: (filled later if this response pivoted from interruption)

**Example Flow**:
```
User:   "Tell me about Python"
Assistant: [Speaking] "Python is a high-level programming language known for its..."
User:   [Interrupts] "Wait, which version?"
Assistant: [Pivot] "Good question! I was explaining Python in general, but let me focus on versions. Python 3.x is the current..."
```

**Database Schema**:
```python
# New fields added to ThoughtLog model:
- interrupted_by: TextField  # Partial response that was cut off
- interruption_type: CharField  # "user_speech", "background_noise", "not_interrupted"
- resumption_context: TextField  # How next response incorporated this
```

---

## Testing Checklist

### Unit Tests:
- [ ] `_estimate_rms_variance()` returns 0 for single value, calculates correctly for list
- [ ] `_is_likely_speech()` returns ("False", "silence") for RMS < 420
- [ ] `_is_likely_speech()` returns ("False", "background_noise") for low variance + steady tone
- [ ] `_is_likely_speech()` returns ("True", "user_speech") for high variance + high RMS
- [ ] Django check passes (no migration errors)

### Integration Tests:
1. **Background Noise Filter**:
   - [ ] Start session, don't speak
   - [ ] Ring a doorbell or play alert sound
   - Debug log should show: `"detection": "background_noise"`
   - Response should NOT be cancelled
   - Check: No "barge_in" event in logs

2. **User Speech Detection**:
   - [ ] Start session, speak clearly
   - Session should allow response creation
   - If assistant is speaking, should cancel on new speech
   - Debug log should show: `"detection": "user_speech"`
   - Check: "barge_in" event appears in logs

3. **Response Interruption Persistence**:
   - [ ] Start session, ask a question
   - [ ] Wait for assistant to start responding
   - [ ] Interrupt mid-sentence with a new question
   - [ ] Check database: New ThoughtLog row created with:
     - `interrupted_by`: Non-empty (partial previous response)
     - `interruption_type`: "user_speech"
   - [ ] Check logs: "barge_in" event with `"was_saying"` field

4. **Response Resumption Context**:
   - [ ] After interruption, ask related follow-up question
   - [ ] Monitor debug logs for response instructions
   - [ ] Should see: "INTERRUPTION CONTEXT: I was saying..."
   - [ ] Assistant should naturally incorporate both contexts
   - [ ] Check database: Next ThoughtLog shows thoughtful pivot

### Manual Testing:
1. Open frontend, start conversation
2. During assistant's response, simulate:
   - a) Doorbell ring (steady tone)
   - b) User interrupt (speech)
   - c) Background music (random variations)
3. Observe how assistant handles each
4. Query database to verify persistence

### Debug Monitoring:
- Watch debug panel for "detection" field (should show user_speech, background_noise, or silence)
- Monitor "barge_in" events
- Check RMS values and variance calculations
- Verify ThoughtLog rows are created with correct interruption_type

---

## Database Migration

Migration applied: `core/0006_thoughtlog_interrupted_by_and_more.py`

Fields added:
```sql
ALTER TABLE core_thoughtlog ADD COLUMN interrupted_by TEXT DEFAULT '' NOT NULL;
ALTER TABLE core_thoughtlog ADD COLUMN interruption_type VARCHAR(32) DEFAULT '' NOT NULL;
ALTER TABLE core_thoughtlog ADD COLUMN resumption_context TEXT DEFAULT '' NOT NULL;
```

---

## Configuration Constants

All in `core/consumers.py`:

```python
VAD_THRESHOLD = 420.0  # RMS energy threshold for detecting sound
RMS_VARIANCE_THRESHOLD = 5000  # Min variance to classify as speech (not steady background)
ENERGY_GRADIENT_MIN = 50  # Min energy change between chunks
RMS_HISTORY_WINDOW = 10  # Chunks to track for variance
```

Tune these if detection isn't working well for your environment.

---

## Known Limitations

1. **Low variance detection** assumes doorbell/alerts have constant energy
   - May fail on: Pulsing/varying doorbell, rhythmic music
   - Solution: Could add frequency analysis in future

2. **Interruption context limited to 150 chars**
   - Prevents overwhelming the system prompt
   - Full interrupted response still stored in database

3. **Single interruption tracking**
   - Only tracks most recent interruption per turn
   - Could extend to track multiple interruptions

4. **No ML model dependency**
   - Uses simple heuristics (variance + energy)
   - More accurate than simple threshold, less accurate than ML
   - Tradeoff: Lightweight vs perfect accuracy

---

## Future Enhancements

1. Add frequency spectrum analysis to improve noise detection
2. Implement interruption recovery (continue from exact byte offset)
3. Multi-turn interruption history in single response
4. Adaptive RMS threshold based on environment baseline
5. Integration with speech duration patterns
6. Persistent environment profile (learn what normal background noise is)


/** Recording hours always use Beijing time, independent of the computer's timezone. */
export interface RecordingSchedule {
  enableRecordingHours?: boolean
  applyToDays?: 'weekday' | 'everyday'
  recordingHours?: readonly string[]
}

const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000

function parseTime(value: string): number | null {
  const match = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value)
  if (!match) return null
  const hour = Number(match[1])
  const minute = Number(match[2])
  const second = Number(match[3] ?? 0)
  if (hour > 23 || minute > 59 || second > 59) return null
  return hour * 3600 + minute * 60 + second
}

export function canRecordInBeijing(schedule: RecordingSchedule, nowMs = Date.now()): boolean {
  if (!schedule.enableRecordingHours) return true
  const hours = schedule.recordingHours
  if (!hours || hours.length !== 2 || hours.some((value) => typeof value !== 'string')) return false
  const start = parseTime(hours[0])
  const end = parseTime(hours[1])
  if (start === null || end === null || !Number.isFinite(nowMs)) return false

  // Read UTC fields after an explicit UTC+8 shift; never use local clock fields or DST.
  const now = new Date(nowMs + BEIJING_OFFSET_MS)
  const day = now.getUTCDay()
  if (schedule.applyToDays === 'weekday' && (day === 0 || day === 6)) return false
  const time = now.getUTCHours() * 3600 + now.getUTCMinutes() * 60 + now.getUTCSeconds()

  // Start is inclusive and end is exclusive, including windows crossing midnight.
  return start > end ? time >= start || time < end : time >= start && time < end
}

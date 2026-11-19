const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const ts = require('typescript')

// Run the actual shared helper without adding a test runtime or changing production builds.
const filename = path.join(__dirname, '../packages/shared/recording-schedule.ts')
const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText
const loaded = { exports: {} }
new Function('module', 'exports', compiled)(loaded, loaded.exports)
const { canRecordInBeijing } = loaded.exports
const schedule = { enableRecordingHours: true, applyToDays: 'everyday', recordingHours: ['08:00:00', '20:00:00'] }
const at = (iso, config = schedule) => canRecordInBeijing(config, Date.parse(iso))

test('Beijing boundaries: 08:00 inclusive and 20:00 exclusive', () => {
  assert.equal(at('2026-10-06T23:59:59Z'), false)
  assert.equal(at('2026-10-07T00:00:00Z'), true)
  assert.equal(at('2026-10-07T11:59:59Z'), true)
  assert.equal(at('2026-10-07T12:00:00Z'), false)
  assert.equal(at('2026-10-07T23:59:59Z'), false)
  assert.equal(at('2026-10-08T00:00:00Z'), true)
})

test('accepts HH:mm and strictly validates HH:mm:ss', () => {
  assert.equal(at('2026-10-07T00:00:00Z', { ...schedule, recordingHours: ['08:00', '20:00'] }), true)
  const seconds = { ...schedule, recordingHours: ['08:00:30', '20:00:30'] }
  assert.equal(at('2026-10-07T00:00:29Z', seconds), false)
  assert.equal(at('2026-10-07T00:00:30Z', seconds), true)
  assert.equal(at('2026-10-07T12:00:29Z', seconds), true)
  assert.equal(at('2026-10-07T12:00:30Z', seconds), false)
  for (const value of ['24:00', '08:60', '08:00:60', '8:00', '08:00junk', '']) {
    assert.equal(at('2026-10-07T00:00:00Z', { ...schedule, recordingHours: [value, '20:00'] }), false)
  }
  assert.equal(at('2026-10-07T00:00:00Z', { ...schedule, recordingHours: undefined }), false)
})

test('weekdays use the Beijing date even when the US date is different', () => {
  const weekdays = { ...schedule, applyToDays: 'weekday' }
  assert.equal(at('2026-10-04T23:59:59Z', weekdays), false) // Beijing Monday before 08:00
  assert.equal(at('2026-10-05T00:00:00Z', weekdays), true) // US Sunday, Beijing Monday
  assert.equal(at('2026-10-10T00:00:00Z', weekdays), false) // US Friday, Beijing Saturday
})

test('cross-midnight windows retain the inclusive start and exclusive end', () => {
  const overnight = { ...schedule, recordingHours: ['22:00', '06:00'] }
  assert.equal(at('2026-10-07T13:59:59Z', overnight), false)
  assert.equal(at('2026-10-07T14:00:00Z', overnight), true)
  assert.equal(at('2026-10-07T17:00:00Z', overnight), true)
  assert.equal(at('2026-10-07T21:59:59Z', overnight), true)
  assert.equal(at('2026-10-07T22:00:00Z', overnight), false)
})

test('disabled recording hours allow recording; equal endpoints define an empty window', () => {
  assert.equal(at('2026-10-07T12:00:00Z', { enableRecordingHours: false }), true)
  assert.equal(at('2026-10-07T00:00:00Z', { ...schedule, recordingHours: ['08:00', '08:00'] }), false)
})

test('same instants give the same result in US daylight and winter timezones', () => {
  const originalTimezone = process.env.TZ
  try {
    for (const zone of ['America/Los_Angeles', 'America/New_York', 'Asia/Shanghai', 'UTC']) {
      process.env.TZ = zone
      for (const day of ['2026-07-07', '2026-12-07']) {
        assert.equal(at(`${day}T00:00:00Z`), true, `${zone} ${day} Beijing 08:00`)
        assert.equal(at(`${day}T11:59:59Z`), true, `${zone} ${day} Beijing 19:59:59`)
        assert.equal(at(`${day}T12:00:00Z`), false, `${zone} ${day} Beijing 20:00`)
      }
      assert.equal(at('2026-03-08T09:59:59Z'), true) // US DST starts
      assert.equal(at('2026-03-08T10:00:00Z'), true)
      assert.equal(at('2026-11-01T08:59:59Z'), true) // US DST ends
      assert.equal(at('2026-11-01T09:00:00Z'), true)
    }
  } finally {
    if (originalTimezone === undefined) delete process.env.TZ
    else process.env.TZ = originalTimezone
  }
})

import { useState, useEffect } from 'react'
import { useMemoizedFn } from 'ahooks'
import { canRecordInBeijing } from '@shared/recording-schedule'
import { ApplyToDays } from '@renderer/store/setting'

export const useRecordingTimeValidation = (
  enableRecordingHours: boolean,
  recordingHours: [string, string],
  applyToDays: ApplyToDays,
  isMonitoring: boolean
) => {
  const [canRecord, setCanRecord] = useState(false)

  const checkCanRecord = useMemoizedFn(() => {
    const result = canRecordInBeijing({ enableRecordingHours, recordingHours, applyToDays })
    setCanRecord(result)
    return result
  })
  //
  //   // Check recording status on mount
  useEffect(() => {
    checkCanRecord()
  }, [checkCanRecord, enableRecordingHours, recordingHours, applyToDays])
  //   // Periodically check recording status
  useEffect(() => {
    let interval: NodeJS.Timeout | null = null
    if (isMonitoring && enableRecordingHours) {
      interval = setInterval(() => {
        checkCanRecord()
      }, 60000) // Check every minute
    }
    return () => {
      if (interval) {
        clearInterval(interval)
      }
    }
  }, [isMonitoring, enableRecordingHours, checkCanRecord])

  return { canRecord, checkCanRecord }
}

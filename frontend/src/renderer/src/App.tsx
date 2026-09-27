// Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: Apache-2.0

import React, { useState, useEffect, useRef } from 'react'
import store, { persistor, useAppSelector } from '@renderer/store'
import { Provider } from 'react-redux'
import { PersistGate } from 'redux-persist/integration/react'

import { ConfigProvider } from '@arco-design/web-react'
import enUS from '@arco-design/web-react/es/locale/en-US'
import zhCN from '@arco-design/web-react/es/locale/zh-CN'
import '@renderer/assets/main.css'
import '@renderer/assets/theme/index.less'
import 'allotment/dist/style.css'
import LoadingComponent from './components/Loading'
import { NotificationProvider } from './context/NotificationProvider'
import Router from './Router'
import { BackendStatus } from './components/Loading'
import { CaptureSourcesProvider } from './atom/capture.atom'
import Settings from './pages/settings/settings'
import { ServiceProvider, useObservableTask } from './atom/event-loop.atom'
import { getLogger } from '@shared/logger/renderer'
import { useMemoizedFn } from 'ahooks'
import axiosInstance from '@renderer/services/axiosConfig'

const logger = getLogger('App.tsx')
const isEnglish = true // Hardcode for now, will change later
interface BackendStatusInfo {
  status: BackendStatus
  port: number
  timestamp: string
}

function AppContent(): React.ReactElement {
  const [backendStatus, setBackendStatus] = useState<BackendStatus>('starting')
  const [showSetting, setShowSettings] = useState<boolean>(false)
  const [setupChecked, setSetupChecked] = useState<boolean>(false)
  const screenSettings = useAppSelector((state) => state.setting.screenSettings)
  const autoStartAttemptedRef = useRef(false)

  const loadBackendHealth = useMemoizedFn(async () => {
    const response = await axiosInstance.get('/api/health')
    const health = response.data
    if (health?.code !== 0 || health?.data?.status !== 'healthy') {
      return false
    }

    setBackendStatus('running')
    setShowSettings(!health.data.components.llm)
    setSetupChecked(true)
    return true
  })

  const checkInitialStatus = useMemoizedFn(async () => {
    let statusInfo: BackendStatusInfo | null = null
    try {
      statusInfo = await window.electron.ipcRenderer.invoke('backend:get-status')
    } catch (error) {
      logger.error('Failed to get initial backend status:', { error })
    }

    try {
      if (await loadBackendHealth()) {
        return
      }
    } catch (error) {
      logger.error('Failed to verify backend health:', { error })
    }

    setBackendStatus(statusInfo?.status || 'error')
  })
  const statusCheckIntervalRef = useRef<NodeJS.Timeout | null>(null)
  const clearStatusCheckInterval = useMemoizedFn(() => {
    if (statusCheckIntervalRef.current) {
      clearTimeout(statusCheckIntervalRef.current)
      statusCheckIntervalRef.current = null
    }
  })
  const scheduleNextCheck = useMemoizedFn(() => {
    statusCheckIntervalRef.current = setTimeout(() => {
      if (backendStatus !== 'running') {
        checkInitialStatus()
      } else {
        clearStatusCheckInterval()
      }
      scheduleNextCheck()
    }, 3000)
  })
  useEffect(() => {
    const handleStatusChange = (_event: any, statusInfo: BackendStatusInfo) => {
      logger.info('Backend status changed:', statusInfo)
      setBackendStatus(statusInfo.status)
      if (statusInfo.status === 'running') {
        clearStatusCheckInterval()
      }
    }

    checkInitialStatus()
    // Listen for status change events
    window.electron.ipcRenderer.on('backend:status-changed', handleStatusChange)

    scheduleNextCheck()

    return () => {
      clearStatusCheckInterval()
      window.electron.ipcRenderer.removeAllListeners('backend:status-changed')
    }
  }, [])

  useObservableTask({
    active: () => {
      logger.info('🔓 Screen unlocked, check backend sever initial status')
      checkInitialStatus()
    },
    inactive: () => {
      logger.info('🔒 Screen locked, stop check backend sever initial status')
      clearStatusCheckInterval()
    }
  })

  useEffect(() => {
    window.serverPushAPI.getInitCheckData((data) => {
      const temp = JSON.parse(data)
      logger.info('Init settings data:', temp)
      setShowSettings(!temp.data.components.llm)
      setSetupChecked(true)
    })
  }, [])

  useEffect(() => {
    if (backendStatus !== 'running') return

    let cancelled = false
    const loadSetupStatus = async () => {
      for (let attempt = 0; attempt < 3 && !cancelled; attempt++) {
        try {
          if (await loadBackendHealth()) return
        } catch (error) {
          logger.error('Failed to load setup status:', { error, attempt: attempt + 1 })
          if (attempt < 2) {
            await new Promise((resolve) => window.setTimeout(resolve, 1000))
          }
        }
      }
      if (!cancelled) {
        setBackendStatus('error')
      }
    }

    loadSetupStatus()
    return () => {
      cancelled = true
    }
  }, [backendStatus, loadBackendHealth])

  useEffect(() => {
    if (backendStatus !== 'running' || !setupChecked || showSetting || autoStartAttemptedRef.current) {
      return
    }

    autoStartAttemptedRef.current = true

    const startScreenMonitoring = async () => {
      const [savedSources, captureResult] = await Promise.all([
        window.screenMonitorAPI.getSettings<{
          screenList?: Array<{ id: string }>
          windowList?: Array<{ id: string }>
        }>('settings'),
        window.screenMonitorAPI.getCaptureAllSources()
      ])

      if (!captureResult.success) {
        throw new Error(captureResult.error || 'Failed to load capture sources')
      }

      const allSources = captureResult.sources || []
      const savedIds = new Set([
        ...(savedSources?.screenList || []).map((source) => source.id),
        ...(savedSources?.windowList || []).map((source) => source.id)
      ])
      const selectedSources = allSources.filter((source) => savedIds.has(source.id))

      if (!selectedSources.some((source) => source.type === 'screen')) {
        const firstScreen = allSources.find((source) => source.type === 'screen')
        if (firstScreen) selectedSources.unshift(firstScreen)
      }

      if (selectedSources.length === 0) {
        throw new Error('No screen is available for recording')
      }

      await window.screenMonitorAPI.updateModelConfig(screenSettings)
      await window.screenMonitorAPI.updateCurrentRecordApp(selectedSources)
      await window.screenMonitorAPI.startTask()
      logger.info('Screen monitoring started automatically', {
        recordInterval: screenSettings.recordInterval,
        recordingHours: screenSettings.recordingHours,
        applyToDays: screenSettings.applyToDays
      })
    }

    startScreenMonitoring().catch((error) => {
      logger.error('Failed to start screen monitoring automatically:', error)
    })
  }, [backendStatus, setupChecked, showSetting, screenSettings])

  // Decide whether to display the application based on the status
  const pythonServerStarted = backendStatus === 'running'
  const closeSetting = useMemoizedFn(() => {
    setShowSettings(false)
  })
  return (
    <>
      {pythonServerStarted && setupChecked ? (
        <CaptureSourcesProvider>
          {showSetting ? <Settings closeSetting={closeSetting} init /> : <Router />}
        </CaptureSourcesProvider>
      ) : (
        <LoadingComponent backendStatus={backendStatus} />
      )}
    </>
  )
}

function App(): React.ReactElement {
  return (
    <Provider store={store}>
      <ConfigProvider locale={isEnglish ? enUS : zhCN}>
        <ServiceProvider>
          <NotificationProvider>
            <PersistGate loading={null} persistor={persistor}>
              <AppContent />
            </PersistGate>
          </NotificationProvider>
        </ServiceProvider>
      </ConfigProvider>
    </Provider>
  )
}

export default App

// Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: Apache-2.0

import axiosInstance from '@renderer/services/axiosConfig'
import { get } from 'lodash'

// Model configuration interface
export interface ModelConfigProps {
  modelPlatform: string // Model platform, e.g., doubao, openai, custom
  modelId: string // VLM model ID
  backgroundModelId?: string // Optional separate model for background text processing
  visionModelId?: string // Optional separate model for image and screenshot analysis
  reasoningEffort?: string // Reasoning effort for chat/retrieval
  backgroundReasoningEffort?: string // Reasoning effort for background text processing
  visionReasoningEffort?: string // Reasoning effort for image and screenshot analysis
  baseUrl: string // API base URL
  embeddingModelId: string // Embedding model ID
  apiKey: string // API key
  embeddingBaseUrl?: string // Optional separate embedding base URL
  embeddingApiKey?: string // Optional separate embedding API key
  embeddingModelPlatform?: string // Optional separate embedding platform
  // Keep the currently configured embedding dimension when switching VLM providers.
  embeddingOutputDim?: number
  output_dim?: number
}

// API response data structure
export interface ModelInfoResponseData {
  config: ModelConfigProps
}

export interface ChatGPTAuthStatus {
  authenticated: boolean
  accountId?: string
  email?: string
  planType?: string
  loginId?: string
  message?: string
}

export interface ChatGPTAuthLoginStart {
  loginId: string
  authUrl?: string
  status?: string
  message?: string
}

export interface ChatGPTModelInfo {
  id: string
  name?: string
  label?: string
  isDefault?: boolean
  inputModalities?: string[]
  supportedReasoningEfforts?: string[]
  defaultReasoningEffort?: string
}

// Complete API response structure
export interface ApiResponse<T> {
  code: number
  status: number
  message: string
  data: T
}

// Get model settings information
export const getModelInfo = async (): Promise<ModelInfoResponseData | undefined> => {
  const res = await axiosInstance.get<ModelInfoResponseData>('/api/model_settings/get')
  return get(res, 'data.data')
}

// Update model settings information
// Update model settings information response data structure
export interface UpdateModelSettingsResponseData {
  success: boolean
  message: string
}

export const updateModelSettingsAPI = async (
  params: ModelConfigProps
): Promise<UpdateModelSettingsResponseData | undefined> => {
  const res = await axiosInstance.post<UpdateModelSettingsResponseData>('/api/model_settings/update', {
    config: {
      ...params
    }
  })
  return get(res, 'data.data')
}

// The subscription endpoints are backed by the official Codex App Server in the
// backend. Keeping these calls here lets the settings page remain independent of
// the App Server wire protocol and also makes errors display consistently.
const CHATGPT_SETTINGS_PREFIX = '/api/model_settings/chatgpt'

const getChatGPTData = <T>(response: { data?: { data?: T } }): T | undefined => {
  return get(response, 'data.data') as T | undefined
}

const normalizeAuthStatus = (value: any): ChatGPTAuthStatus | undefined => {
  if (!value) return undefined
  return {
    ...value,
    authenticated: Boolean(value.authenticated ?? value.loggedIn ?? value.logged_in ?? value.isAuthenticated),
    accountId: value.accountId ?? value.account_id,
    planType: value.planType ?? value.plan_type
  }
}

export const getChatGPTAuthStatus = async (): Promise<ChatGPTAuthStatus | undefined> => {
  const res = await axiosInstance.get(`${CHATGPT_SETTINGS_PREFIX}/auth/status`)
  return normalizeAuthStatus(getChatGPTData<ChatGPTAuthStatus>(res))
}

export const startChatGPTLogin = async (): Promise<ChatGPTAuthLoginStart | undefined> => {
  const res = await axiosInstance.post(`${CHATGPT_SETTINGS_PREFIX}/auth/login/start`)
  const value = getChatGPTData<ChatGPTAuthLoginStart>(res) as any
  if (!value) return undefined
  return {
    ...value,
    loginId: value.loginId ?? value.login_id ?? '',
    authUrl: value.authUrl ?? value.auth_url
  }
}

export const cancelChatGPTLogin = async (loginId?: string): Promise<void> => {
  await axiosInstance.post(`${CHATGPT_SETTINGS_PREFIX}/auth/login/cancel`, loginId ? { loginId } : {})
}

export const logoutChatGPT = async (): Promise<void> => {
  await axiosInstance.post(`${CHATGPT_SETTINGS_PREFIX}/auth/logout`)
}

export const getChatGPTModels = async (): Promise<ChatGPTModelInfo[]> => {
  const res = await axiosInstance.get(`${CHATGPT_SETTINGS_PREFIX}/models`)
  const data = getChatGPTData<ChatGPTModelInfo[] | { models?: ChatGPTModelInfo[] }>(res)
  const models = Array.isArray(data) ? data : (data?.models ?? [])
  return models.map((model: any) => {
    const rawEfforts =
      model.supportedReasoningEfforts ?? model.supported_reasoning_efforts ?? model.reasoningEfforts ?? model.reasoning_efforts
    const supportedReasoningEfforts = Array.isArray(rawEfforts)
      ? rawEfforts
          .map((effort: any) =>
            typeof effort === 'string' ? effort : effort?.reasoningEffort ?? effort?.reasoning_effort ?? effort?.effort ?? effort?.value
          )
          .filter(Boolean)
      : undefined
    return {
      ...model,
      inputModalities: model.inputModalities ?? model.input_modalities,
      supportedReasoningEfforts,
      defaultReasoningEffort: model.defaultReasoningEffort ?? model.default_reasoning_effort
    }
  })
}

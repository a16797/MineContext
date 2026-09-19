// Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: Apache-2.0

import { FC, useMemo, useEffect, useState } from 'react'
import { Form, Button, Select, Input, Typography, Spin, Message, Tag } from '@arco-design/web-react'
import type { FormInstance } from '@arco-design/web-react'
import { find, get, isEmpty } from 'lodash'

import ModelRadio from './components/modelRadio/model-radio'
import { ModelTypeList, BaseUrl, embeddingModels, ModelInfoList } from './constants'
import {
  cancelChatGPTLogin,
  ChatGPTAuthStatus,
  ChatGPTModelInfo,
  getChatGPTAuthStatus,
  getChatGPTModels,
  getModelInfo,
  logoutChatGPT,
  ModelConfigProps,
  startChatGPTLogin,
  updateModelSettingsAPI
} from '../../services/Settings'
import { useMemoizedFn, useMount, useRequest } from 'ahooks'

const FormItem = Form.Item
const { Text } = Typography

interface SettingsProps {
  closeSetting?: () => void
  init?: boolean
}
export interface InputPrefixProps {
  label: string
}
const InputPrefix: FC<InputPrefixProps> = (props) => {
  const { label } = props
  return <div className="flex w-[73px] items-center">{label}</div>
}
export interface CustomFormItemsProps {
  prefix: string
}
const CustomFormItems: FC<CustomFormItemsProps> = (props) => {
  const { prefix } = props
  return (
    <>
      <div className="flex flex-col gap-6 mb-6">
        <div className="flex flex-col gap-[8px]">
          <span className="text-[#0B0B0F] font-roboto text-base font-normal leading-[22px] ">
            Vision language model
          </span>
          <FormItem
            field={`${prefix}-modelId`}
            className="!mb-0"
            rules={[{ required: true, message: 'Cannot be empty' }]}
            requiredSymbol={false}>
            <Input
              addBefore={<InputPrefix label="Model name" />}
              placeholder="A VLM model with visual understanding capabilities is required."
              allowClear
              className="[&_.arco-input-inner-wrapper]: !w-[574px]"
            />
          </FormItem>
          <FormItem
            field={`${prefix}-baseUrl`}
            className="!mb-0"
            rules={[{ required: true, message: 'Cannot be empty' }]}
            requiredSymbol={false}>
            <Input
              addBefore={<InputPrefix label="Base URL" />}
              placeholder="Enter your base URL"
              allowClear
              className="[&_.arco-input-inner-wrapper]: !w-[574px]"
            />
          </FormItem>
          <FormItem
            field={`${prefix}-apiKey`}
            className="!mb-0"
            rules={[{ required: true, message: 'Cannot be empty' }]}
            requiredSymbol={false}>
            <Input.Password
              addBefore={<InputPrefix label="API Key" />}
              placeholder="Enter your API Key"
              allowClear
              className="!w-[574px]"
              defaultVisibility={false}
            />
          </FormItem>
        </div>
        <div className="flex flex-col gap-[8px]">
          <span className="text-[#0B0B0F] font-roboto text-base font-normal leading-[22px]">Embedding model</span>
          <FormItem
            field={`${prefix}-embeddingModelId`}
            className="!mb-0"
            rules={[{ required: true, message: 'Cannot be empty' }]}
            requiredSymbol={false}>
            <Input
              addBefore={<InputPrefix label="Model name" />}
              placeholder="Enter your embedding model name"
              allowClear
              className="!w-[574px]"
            />
          </FormItem>
          <FormItem
            field={`${prefix}-embeddingBaseUrl`}
            className="!mb-0"
            rules={[{ required: true, message: 'Cannot be empty' }]}
            requiredSymbol={false}>
            <Input
              addBefore={<InputPrefix label="Base URL" />}
              placeholder="Enter your base URL"
              allowClear
              className="!w-[574px]"
            />
          </FormItem>
          <FormItem
            field={`${prefix}-embeddingApiKey`}
            className="!mb-0"
            rules={[{ required: true, message: 'Cannot be empty' }]}
            requiredSymbol={false}>
            <Input.Password
              addBefore={<InputPrefix label="API Key" />}
              placeholder="Enter your API Key"
              allowClear
              className="!w-[574px]"
              defaultVisibility={false}
            />
          </FormItem>
        </div>
      </div>
    </>
  )
}
export interface StandardFormItemsProps {
  modelPlatform: ModelTypeList
  prefix: string
}
const StandardFormItems: FC<StandardFormItemsProps> = (props) => {
  const { modelPlatform, prefix } = props
  const option = useMemo(() => {
    const foundItem = find(ModelInfoList, (item) => item.value === modelPlatform)
    return foundItem ? foundItem.option : []
  }, [modelPlatform])

  return (
    <>
      <FormItem
        label="Select AI model"
        field={`${prefix}-modelId`}
        requiredSymbol={false}
        rules={[
          {
            validator(value, callback) {
              if (!value) {
                callback('Please select AI model')
              } else {
                callback()
              }
            }
          }
        ]}>
        <Select allowCreate placeholder="please select" options={option} className="!w-[574px]" />
      </FormItem>
      <FormItem
        requiredSymbol={false}
        label="API Key"
        field={`${prefix}-apiKey`}
        extra={
          <div className="flex items-center text-[#6E718C] text-[14px] ">
            You can get the API Key Here:
            <Button
              onClick={() => {
                const url =
                  modelPlatform === ModelTypeList.Doubao
                    ? 'https://www.volcengine.com/docs/82379/1541594'
                    : 'https://platform.openai.com/settings/organization/api-keys'
                window.open(`${url}`)
              }}
              type="text">
              {modelPlatform === ModelTypeList.Doubao ? 'Get Doubao API Key' : 'Get OpenAI API Key'}
            </Button>
          </div>
        }
        rules={[
          {
            validator(value, callback) {
              if (!value) {
                callback('Please enter your API key')
              } else {
                callback()
              }
            }
          }
        ]}>
        <Input.Password
          autoFocus
          placeholder="Enter your API key"
          allowClear
          className="!w-[574px]"
          defaultVisibility={false}
        />
      </FormItem>
    </>
  )
}

interface SubscriptionFormItemsProps {
  prefix: string
  form: FormInstance
  authStatus?: ChatGPTAuthStatus
  authError?: boolean
  authLoading: boolean
  models: ChatGPTModelInfo[]
  modelsLoading: boolean
  loginLoading: boolean
  loginId?: string
  onLogin: () => void
  onCancelLogin: () => void
  onLogout: () => void
  onRefresh: () => void
}

const SubscriptionFormItems: FC<SubscriptionFormItemsProps> = (props) => {
  const {
    prefix,
    form,
    authStatus,
    authError,
    authLoading,
    models,
    modelsLoading,
    loginLoading,
    loginId,
    onLogin,
    onCancelLogin,
    onLogout,
    onRefresh
  } = props
  const textModelId = Form.useWatch(`${prefix}-modelId`, form) as string | undefined
  const backgroundModelId = Form.useWatch(`${prefix}-backgroundModelId`, form) as string | undefined
  const visionModelId = Form.useWatch(`${prefix}-visionModelId`, form) as string | undefined

  const supportsModality = (model: ChatGPTModelInfo, modality: string) => {
    if (!model.inputModalities || model.inputModalities.length === 0) return true
    return model.inputModalities.some((item) => item.toLowerCase() === modality)
  }
  const modelOptions = (modality: string) =>
    models
      .filter((model) => supportsModality(model, modality))
      .map((model) => ({
        value: model.id,
        label: model.label || model.name || model.id
      }))
  const effortOptions = (modelId: string | undefined) => {
    const model = models.find((item) => item.id === modelId)
    const supported = model?.supportedReasoningEfforts?.length
      ? model.supportedReasoningEfforts
      : model?.defaultReasoningEffort
        ? [model.defaultReasoningEffort]
        : []
    return supported.map((effort) => ({ value: effort, label: effort }))
  }
  const resetEffortForModel = (modelId: string, fieldName: string) => {
    const model = models.find((item) => item.id === modelId)
    const supported = model?.supportedReasoningEfforts || []
    const current = form.getFieldValue(fieldName)
    if (supported.includes(current)) return
    const next = supported.includes('medium') ? 'medium' : model?.defaultReasoningEffort || supported[0]
    form.setFieldValue(fieldName, next)
  }
  const textOptions = modelOptions('text')
  const visionOptions = modelOptions('image')
  const authenticated = !!authStatus?.authenticated
  const statusUnavailable = !!authError

  return (
    <div className="flex flex-col gap-6 mb-6">
      <div className="flex flex-col gap-[8px]">
        <span className="text-[#0B0B0F] font-roboto text-base font-normal leading-[22px]">ChatGPT subscription</span>
        <div className="flex items-center gap-2 text-[14px] text-[#6E718C]">
          <Tag color={authenticated ? 'green' : 'gray'}>
            {authenticated ? 'Connected' : statusUnavailable ? 'Status unavailable' : 'Not connected'}
          </Tag>
          <span>
            {authenticated
              ? authStatus?.email || authStatus?.planType || 'ChatGPT account connected'
              : statusUnavailable
                ? 'Could not read the saved login. Refresh the status to try again.'
              : 'Sign in with the official Codex login to use your ChatGPT subscription.'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {authenticated ? (
            <Button type="outline" onClick={onLogout} loading={loginLoading}>
              Sign out
            </Button>
          ) : statusUnavailable ? null : (
            <Button type="primary" onClick={onLogin} loading={loginLoading}>
              Sign in with ChatGPT
            </Button>
          )}
          {loginId && !authenticated && (
            <Button type="text" onClick={onCancelLogin} disabled={loginLoading}>
              Cancel login
            </Button>
          )}
          <Button type="text" onClick={onRefresh} loading={authLoading}>
            Refresh status
          </Button>
        </div>
      </div>

      <FormItem
        label="Chat model"
        field={`${prefix}-modelId`}
        requiredSymbol={false}
        rules={[{ required: true, message: 'Please select a ChatGPT model for text and chat' }]}>
        <Select
          placeholder={authenticated ? 'Please select' : 'Sign in first'}
          options={textOptions}
          loading={modelsLoading}
          disabled={!authenticated || modelsLoading}
          onChange={(value) => resetEffortForModel(value, `${prefix}-reasoningEffort`)}
          className="!w-[574px]"
        />
      </FormItem>

      <FormItem
        label="Chat reasoning strength"
        field={`${prefix}-reasoningEffort`}
        requiredSymbol={false}
        rules={[{ required: true, message: 'Please select a reasoning strength for chat' }]}
        extra={<span className="text-[#6E718C]">Only the strengths supported by the selected model are shown.</span>}>
        <Select
          placeholder={authenticated ? 'Please select' : 'Sign in first'}
          options={effortOptions(textModelId)}
          disabled={!authenticated || modelsLoading || !textModelId}
          className="!w-[574px]"
        />
      </FormItem>

      <FormItem
        label="Background text model"
        field={`${prefix}-backgroundModelId`}
        requiredSymbol={false}
        rules={[{ required: true, message: 'Please select a model for background text processing' }]}
        extra={<span className="text-[#6E718C]">Used for summaries, reports, todos and other background text processing.</span>}>
        <Select
          placeholder={authenticated ? 'Please select' : 'Sign in first'}
          options={textOptions}
          loading={modelsLoading}
          disabled={!authenticated || modelsLoading}
          onChange={(value) => resetEffortForModel(value, `${prefix}-backgroundReasoningEffort`)}
          className="!w-[574px]"
        />
      </FormItem>

      <FormItem
        label="Background reasoning strength"
        field={`${prefix}-backgroundReasoningEffort`}
        requiredSymbol={false}
        rules={[{ required: true, message: 'Please select a reasoning strength for background processing' }]}
        extra={<span className="text-[#6E718C]">Used by background text tasks only.</span>}>
        <Select
          placeholder={authenticated ? 'Please select' : 'Sign in first'}
          options={effortOptions(backgroundModelId)}
          disabled={!authenticated || modelsLoading || !backgroundModelId}
          className="!w-[574px]"
        />
      </FormItem>

      <FormItem
        label="Image / screenshot model"
        field={`${prefix}-visionModelId`}
        requiredSymbol={false}
        rules={[{ required: true, message: 'Please select a ChatGPT model for image analysis' }]}
        extra={<span className="text-[#6E718C]">Used only when a request contains images. Screenshot analysis is routed here automatically.</span>}>
        <Select
          placeholder={authenticated ? 'Please select' : 'Sign in first'}
          options={visionOptions}
          loading={modelsLoading}
          disabled={!authenticated || modelsLoading}
          onChange={(value) => resetEffortForModel(value, `${prefix}-visionReasoningEffort`)}
          className="!w-[574px]"
        />
      </FormItem>

      <FormItem
        label="Screenshot reasoning strength"
        field={`${prefix}-visionReasoningEffort`}
        requiredSymbol={false}
        rules={[{ required: true, message: 'Please select a reasoning strength for image analysis' }]}
        extra={<span className="text-[#6E718C]">Used for screenshot analysis only.</span>}>
        <Select
          placeholder={authenticated ? 'Please select' : 'Sign in first'}
          options={effortOptions(visionModelId)}
          disabled={!authenticated || modelsLoading || !visionModelId}
          className="!w-[574px]"
        />
      </FormItem>

      <div className="flex flex-col gap-[8px]">
        <span className="text-[#0B0B0F] font-roboto text-base font-normal leading-[22px]">Embedding model</span>
        <FormItem
          label="Provider"
          field={`${prefix}-embeddingModelPlatform`}
          requiredSymbol={false}
          rules={[{ required: true, message: 'Please select an embedding provider' }]}>
          <Select
            options={[
              { value: ModelTypeList.Doubao, label: 'Doubao' },
              { value: ModelTypeList.OpenAI, label: 'OpenAI' },
              { value: ModelTypeList.Custom, label: 'Custom' }
            ]}
            className="!w-[574px]"
          />
        </FormItem>
        <FormItem
          field={`${prefix}-embeddingModelId`}
          className="!mb-0"
          rules={[{ required: true, message: 'Cannot be empty' }]}
          requiredSymbol={false}>
          <Input
            addBefore={<InputPrefix label="Model name" />}
            placeholder="Keep your existing embedding model"
            allowClear
            className="!w-[574px]"
          />
        </FormItem>
        <FormItem field={`${prefix}-embeddingBaseUrl`} className="!mb-0" requiredSymbol={false}>
          <Input
            addBefore={<InputPrefix label="Base URL" />}
            placeholder="Keep your existing embedding base URL"
            allowClear
            className="!w-[574px]"
          />
        </FormItem>
        <FormItem field={`${prefix}-embeddingApiKey`} className="!mb-0" requiredSymbol={false}>
          <Input.Password
            addBefore={<InputPrefix label="API Key" />}
            placeholder="Keep your existing embedding API key"
            allowClear
            className="!w-[574px]"
            defaultVisibility={false}
          />
        </FormItem>
        <Text type="secondary" className="text-[13px]">
          ChatGPT subscription is used for chat and vision only. Embeddings continue to use this separate configuration.
        </Text>
      </div>
    </div>
  )
}

// 1. Add showCheckIcon state
export interface SettingsFormBase {
  modelPlatform: string
}

export type SettingsFormProps = SettingsFormBase & {
  [key: string]: string | number | undefined
}

interface EmbeddingSettings {
  modelId: string
  baseUrl: string
  apiKey: string
  provider?: string
  outputDim?: number
}
const Settings: FC<SettingsProps> = (props) => {
  const { closeSetting, init } = props

  const [form] = Form.useForm<SettingsFormProps>()
  const [embeddingSettings, setEmbeddingSettings] = useState<EmbeddingSettings | undefined>()
  const [subscriptionLoginId, setSubscriptionLoginId] = useState<string>()
  const { run: getInfo, loading: getInfoLoading, data: modelInfo } = useRequest(getModelInfo, { manual: true })

  const {
    run: refreshSubscriptionStatus,
    runAsync: refreshSubscriptionStatusAsync,
    data: subscriptionStatus,
    loading: subscriptionStatusLoading,
    error: subscriptionStatusError
  } = useRequest(getChatGPTAuthStatus, {
    manual: true,
    onError(error: Error) {
      Message.error(get(error, 'response.data.message') || get(error, 'message') || 'Failed to read ChatGPT login status')
    }
  })
  const {
    run: refreshSubscriptionModels,
    data: subscriptionModels = [],
    loading: subscriptionModelsLoading
  } = useRequest(getChatGPTModels, {
    manual: true,
    onError(error: Error) {
      Message.error(get(error, 'response.data.message') || get(error, 'message') || 'Failed to load ChatGPT models')
    }
  })
  const { run: startLogin, loading: loginLoading } = useRequest(startChatGPTLogin, {
    manual: true,
    onSuccess(result) {
      if (!result) {
        Message.error('ChatGPT login did not return a login URL')
        return
      }
      setSubscriptionLoginId(result.loginId)
      if (result.authUrl) {
        window.open(result.authUrl)
      }
      Message.info(result.authUrl ? 'Complete the ChatGPT login in your browser, then return here.' : 'ChatGPT login started')
      refreshSubscriptionStatus()
    },
    onError(error: Error) {
      Message.error(get(error, 'response.data.message') || get(error, 'message') || 'Failed to start ChatGPT login')
    }
  })
  const { run: cancelLogin } = useRequest(cancelChatGPTLogin, {
    manual: true,
    onSuccess() {
      setSubscriptionLoginId(undefined)
      refreshSubscriptionStatus()
    },
    onError(error: Error) {
      Message.error(get(error, 'response.data.message') || get(error, 'message') || 'Failed to cancel ChatGPT login')
    }
  })
  const { run: logout, loading: logoutLoading } = useRequest(logoutChatGPT, {
    manual: true,
    onSuccess() {
      setSubscriptionLoginId(undefined)
      refreshSubscriptionStatus()
      Message.success('ChatGPT account signed out')
    },
    onError(error: Error) {
      Message.error(get(error, 'response.data.message') || get(error, 'message') || 'Failed to sign out ChatGPT')
    }
  })

  const { run: updateModelSettings, loading: updateLoading } = useRequest(updateModelSettingsAPI, {
    manual: true,
    onSuccess() {
      Message.success('Model settings saved successfully')
      getInfo()
      if (init) {
        closeSetting?.()
      }
    },
    onError(e: Error) {
      const errMsg = get(e, 'response.data.message') || get(e, 'message') || 'Failed to save settings'
      Message.error(errMsg)
    }
  })
  const submit = useMemoizedFn(async () => {
    try {
      await form.validate()
      const values = form.getFieldsValue()
      if (!values.modelPlatform) {
        Message.error('Please select Model Platform')
        return
      }
      const platform = values.modelPlatform as ModelTypeList
      const value = values as Record<string, string | number | undefined>
      const field = (name: string) => value[`${platform}-${name}`]
      const isCustom = platform === ModelTypeList.Custom
      const isSubscription = platform === ModelTypeList.ChatGPTSubscription
      const savedEmbedding = embeddingSettings
      const modelId = String(field('modelId') || '')
      const visionModelId = String(field('visionModelId') || modelId)
      const backgroundModelId = String(field('backgroundModelId') || modelId)
      const apiKey = isSubscription ? '' : String(field('apiKey') || '')
      const baseUrl = isCustom
        ? String(field('baseUrl') || '')
        : platform === ModelTypeList.Doubao
          ? BaseUrl.DoubaoUrl
          : platform === ModelTypeList.OpenAI
            ? BaseUrl.OpenAIUrl
            : ''
      const embeddingModelPlatform = String(
        field('embeddingModelPlatform') || savedEmbedding?.provider || (isSubscription ? ModelTypeList.Doubao : platform)
      )
      const embeddingModelId = String(
        field('embeddingModelId') ||
          savedEmbedding?.modelId ||
          (embeddingModelPlatform === ModelTypeList.Doubao
            ? embeddingModels.DoubaoEmbeddingModelId
            : embeddingModels.OpenAIEmbeddingModelId)
      )
      const embeddingBaseUrl = String(
        field('embeddingBaseUrl') ||
          savedEmbedding?.baseUrl ||
          (embeddingModelPlatform === ModelTypeList.Doubao
            ? BaseUrl.DoubaoUrl
            : embeddingModelPlatform === ModelTypeList.OpenAI
              ? BaseUrl.OpenAIUrl
              : '')
      )
      const embeddingApiKey = String(field('embeddingApiKey') || savedEmbedding?.apiKey || (isSubscription ? '' : apiKey))
      const embeddingOutputDimValue = field('embeddingOutputDim') || savedEmbedding?.outputDim
      const params: ModelConfigProps = {
        modelPlatform: platform,
        modelId,
        backgroundModelId,
        visionModelId,
        reasoningEffort: String(field('reasoningEffort') || ''),
        backgroundReasoningEffort: String(field('backgroundReasoningEffort') || ''),
        visionReasoningEffort: String(field('visionReasoningEffort') || ''),
        baseUrl,
        apiKey,
        embeddingModelId,
        embeddingBaseUrl,
        embeddingApiKey,
        embeddingModelPlatform,
        ...(embeddingOutputDimValue ? { embeddingOutputDim: Number(embeddingOutputDimValue) } : {})
      }

      updateModelSettings(params)
    } catch (error: any) {}
  })

  useMount(() => {
    getInfo()
  })
  useEffect(() => {
    if (form.getFieldValue('modelPlatform') === ModelTypeList.ChatGPTSubscription) {
      refreshSubscriptionStatus()
    }
  }, [form, refreshSubscriptionStatus])
  useEffect(() => {
    if (subscriptionStatus?.authenticated) {
      setSubscriptionLoginId(undefined)
      refreshSubscriptionModels()
    }
  }, [refreshSubscriptionModels, subscriptionStatus?.authenticated])
  useEffect(() => {
    if (!subscriptionLoginId || subscriptionStatus?.authenticated) return
    let stopped = false
    let timer: number | undefined
    const poll = async () => {
      try {
        await refreshSubscriptionStatusAsync()
      } catch {
        // useRequest's onError already reports the failure.
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 2000)
      }
    }
    poll()
    return () => {
      stopped = true
      if (timer) window.clearTimeout(timer)
    }
  }, [refreshSubscriptionStatusAsync, subscriptionLoginId, subscriptionStatus?.authenticated])
  useEffect(() => {
    if (form.getFieldValue('modelPlatform') !== ModelTypeList.ChatGPTSubscription || subscriptionModels.length === 0) {
      return
    }
    const textFieldName = `${ModelTypeList.ChatGPTSubscription}-modelId`
    const backgroundFieldName = `${ModelTypeList.ChatGPTSubscription}-backgroundModelId`
    const visionFieldName = `${ModelTypeList.ChatGPTSubscription}-visionModelId`
    const textEffortFieldName = `${ModelTypeList.ChatGPTSubscription}-reasoningEffort`
    const backgroundEffortFieldName = `${ModelTypeList.ChatGPTSubscription}-backgroundReasoningEffort`
    const visionEffortFieldName = `${ModelTypeList.ChatGPTSubscription}-visionReasoningEffort`
    const selectedTextModel = form.getFieldValue(textFieldName)
    const selectedBackgroundModel = form.getFieldValue(backgroundFieldName)
    const selectedVisionModel = form.getFieldValue(visionFieldName)
    const supports = (model: ChatGPTModelInfo, modality: string) =>
      !model.inputModalities?.length || model.inputModalities.some((item) => item.toLowerCase() === modality)
    const findModel = (preferredId: string, modality: string) =>
      subscriptionModels.find((model) => model.id === preferredId && supports(model, modality)) ||
      subscriptionModels.find((model) => model.isDefault && supports(model, modality)) ||
      subscriptionModels.find((model) => supports(model, modality))
    const defaultTextModel = findModel('gpt-5.6-sol', 'text')
    const defaultBackgroundModel = findModel('gpt-5.6-luna', 'text')
    const defaultVisionModel = findModel('gpt-5.6-luna', 'image')
    const nextValues: Record<string, string> = {}
    if (!selectedTextModel && defaultTextModel) {
      nextValues[textFieldName] = defaultTextModel.id
    }
    if (!selectedBackgroundModel && defaultBackgroundModel) {
      nextValues[backgroundFieldName] = defaultBackgroundModel.id
    }
    if (!selectedVisionModel && defaultVisionModel) {
      nextValues[visionFieldName] = defaultVisionModel.id
    }
    const selectedTextModelInfo = subscriptionModels.find((model) => model.id === selectedTextModel)
    const selectedBackgroundModelInfo = subscriptionModels.find((model) => model.id === selectedBackgroundModel)
    const selectedVisionModelInfo = subscriptionModels.find((model) => model.id === selectedVisionModel)
    const defaultEffort = (model: ChatGPTModelInfo | undefined) => {
      if (!model) return undefined
      if (!model.supportedReasoningEfforts?.length) return model.defaultReasoningEffort || 'medium'
      return model.supportedReasoningEfforts.includes('medium')
        ? 'medium'
        : model.defaultReasoningEffort || model.supportedReasoningEfforts[0]
    }
    if (!form.getFieldValue(textEffortFieldName)) {
      const effort = defaultEffort(selectedTextModelInfo || defaultTextModel)
      if (effort) nextValues[textEffortFieldName] = effort
    }
    if (!form.getFieldValue(backgroundEffortFieldName)) {
      const effort = defaultEffort(selectedBackgroundModelInfo || defaultBackgroundModel)
      if (effort) nextValues[backgroundEffortFieldName] = effort
    }
    if (!form.getFieldValue(visionEffortFieldName)) {
      const effort = defaultEffort(selectedVisionModelInfo || defaultVisionModel)
      if (effort) nextValues[visionEffortFieldName] = effort
    }
    if (Object.keys(nextValues).length > 0) form.setFieldsValue(nextValues)
  }, [form, subscriptionModels])
  useEffect(() => {
    const config = get(modelInfo, 'config')
    if (!getInfoLoading && !isEmpty(config)) {
      const embeddingOutputDim = config.embeddingOutputDim ?? config.output_dim
      setEmbeddingSettings({
        modelId:
          config.embeddingModelId ||
          (config.modelPlatform === ModelTypeList.ChatGPTSubscription
            ? embeddingModels.DoubaoEmbeddingModelId
            : ''),
        baseUrl:
          config.embeddingBaseUrl ||
          (config.modelPlatform === ModelTypeList.ChatGPTSubscription ? BaseUrl.DoubaoUrl : ''),
        apiKey: config.embeddingApiKey || '',
        provider:
          config.embeddingModelPlatform ||
          (config.modelPlatform === ModelTypeList.ChatGPTSubscription ? ModelTypeList.Doubao : config.modelPlatform),
        ...(embeddingOutputDim ? { outputDim: Number(embeddingOutputDim) } : {})
      })

      const settingsValue = new Map<string, string | number>()
      const prefix = config.modelPlatform as ModelTypeList
      settingsValue.set(`modelPlatform`, prefix)
      Object.keys(config).reduce((acc, key) => {
        if (!acc.has(`${prefix}-${key}`) && config[key] !== undefined && config[key] !== null) {
          acc.set(`${prefix}-${key}`, config[key])
        }
        return acc
      }, settingsValue)
      if (embeddingOutputDim) settingsValue.set(`${prefix}-embeddingOutputDim`, Number(embeddingOutputDim))
      if (!config.embeddingModelPlatform && prefix === ModelTypeList.ChatGPTSubscription) {
        settingsValue.set(`${prefix}-embeddingModelPlatform`, ModelTypeList.Doubao)
      }
      if (!config.embeddingModelId && prefix === ModelTypeList.ChatGPTSubscription) {
        settingsValue.set(`${prefix}-embeddingModelId`, embeddingModels.DoubaoEmbeddingModelId)
      }
      if (!config.embeddingBaseUrl && prefix === ModelTypeList.ChatGPTSubscription) {
        settingsValue.set(`${prefix}-embeddingBaseUrl`, BaseUrl.DoubaoUrl)
      }
      form.setFieldsValue(Object.fromEntries(settingsValue))
      if (prefix === ModelTypeList.ChatGPTSubscription) {
        refreshSubscriptionStatus()
      }
    }
  }, [modelInfo, getInfoLoading, refreshSubscriptionStatus])

  return (
    <Spin loading={getInfoLoading} block className="[&_.arco-spin-children]:!h-full !h-full">
      <div className="top-0 left-0 flex flex-col h-full overflow-y-hidden py-2 pr-2 relative">
        <div className="bg-white rounded-[16px] pl-6 flex flex-col h-full overflow-y-auto overflow-x-hidden scrollbar-hide pb-2">
          <div className="mb-[12px]">
            <div className="mt-[26px] mb-[10px] text-[24px] font-bold text-[#000]">Select a AI model to start</div>
            <Text type="secondary" className="text-[13px]">
              Configure AI model and API Key, then you can start MineContext’s intelligent context capability
            </Text>
          </div>

          <div>
            <Form
              autoComplete="off"
              layout={'vertical'}
              form={form}
              onValuesChange={(changedValues) => {
                if (changedValues.modelPlatform) {
                  const nextPlatform = changedValues.modelPlatform as ModelTypeList
                  const nextEmbedding =
                    embeddingSettings ||
                    (nextPlatform === ModelTypeList.ChatGPTSubscription
                      ? {
                          modelId: embeddingModels.DoubaoEmbeddingModelId,
                          baseUrl: BaseUrl.DoubaoUrl,
                          apiKey: '',
                          provider: ModelTypeList.Doubao,
                          outputDim: 2048
                        }
                      : undefined)
                  if (nextEmbedding) {
                    form.setFieldsValue({
                      [`${nextPlatform}-embeddingModelId`]: nextEmbedding.modelId,
                      [`${nextPlatform}-embeddingBaseUrl`]: nextEmbedding.baseUrl,
                      [`${nextPlatform}-embeddingApiKey`]: nextEmbedding.apiKey,
                      [`${nextPlatform}-embeddingModelPlatform`]: nextEmbedding.provider,
                      [`${nextPlatform}-embeddingOutputDim`]: nextEmbedding.outputDim
                    })
                  }
                }
                if (changedValues.modelPlatform === ModelTypeList.ChatGPTSubscription) {
                  refreshSubscriptionStatus()
                }
              }}
              initialValues={{
                modelPlatform: ModelTypeList.Doubao,
                [`${ModelTypeList.Doubao}-modelId`]: 'doubao-seed-1-6-flash-250828',
                [`${ModelTypeList.OpenAI}-modelId`]: 'gpt-5-nano'
              }}>
              <FormItem label="Model platform" field={'modelPlatform'} requiredSymbol={false}>
                <ModelRadio />
              </FormItem>
              <FormItem
                shouldUpdate={(prevValues, currentValues) => prevValues.modelPlatform !== currentValues.modelPlatform}
                noStyle>
                {(values) => {
                  const modelPlatform = values.modelPlatform
                  if (modelPlatform === ModelTypeList.Custom) {
                    return <CustomFormItems prefix={ModelTypeList.Custom} />
                  } else if (modelPlatform === ModelTypeList.ChatGPTSubscription) {
                    return (
                      <SubscriptionFormItems
                        prefix={ModelTypeList.ChatGPTSubscription}
                        form={form as unknown as FormInstance}
                        authStatus={subscriptionStatus}
                        authError={!!subscriptionStatusError}
                        authLoading={subscriptionStatusLoading}
                        models={subscriptionModels}
                        modelsLoading={subscriptionModelsLoading}
                        loginLoading={loginLoading || logoutLoading}
                        loginId={subscriptionLoginId}
                        onLogin={startLogin}
                        onCancelLogin={() => cancelLogin(subscriptionLoginId)}
                        onLogout={logout}
                        onRefresh={() => {
                          refreshSubscriptionStatus(true)
                          if (subscriptionStatus?.authenticated) refreshSubscriptionModels()
                        }}
                      />
                    )
                  } else if (modelPlatform === ModelTypeList.Doubao) {
                    return <StandardFormItems modelPlatform={modelPlatform} prefix={ModelTypeList.Doubao} />
                  } else if (modelPlatform === ModelTypeList.OpenAI) {
                    return <StandardFormItems modelPlatform={modelPlatform} prefix={ModelTypeList.OpenAI} />
                  } else {
                    return null
                  }
                }}
              </FormItem>
            </Form>
            <Spin loading={updateLoading}>
              <Button type="primary" onClick={submit} disabled={updateLoading} className="!bg-[#000]">
                {init ? 'Get started' : 'Save'}
              </Button>
            </Spin>
          </div>
        </div>
      </div>
    </Spin>
  )
}

export default Settings

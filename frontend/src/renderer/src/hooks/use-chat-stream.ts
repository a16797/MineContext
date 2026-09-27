// Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: Apache-2.0

import { useState, useCallback, useRef, useEffect } from 'react'
import {
  ChatMessage,
  ChatStreamRequest,
  StreamEvent,
  WorkflowStage,
  chatStreamService
} from '@renderer/services/ChatStreamService'
import { get } from 'lodash'

export interface ChatState {
  messages: ChatMessage[]
  isLoading: boolean
  currentStage?: WorkflowStage
  progress: number
  sessionId: string
  messageId: number
  error?: string
}

export interface StreamingMessage {
  id: string
  role: 'assistant'
  content: string
  isStreaming: boolean
  stage?: WorkflowStage
  progress: number
  timestamp: string
}

export const useChatStream = () => {
  const [chatState, setChatState] = useState<ChatState>({
    messages: [],
    isLoading: false,
    progress: 0,
    sessionId: chatStreamService.generateSessionId(),
    messageId: -1
  })

  const [streamingMessage, setStreamingMessage] = useState<StreamingMessage | null>(null)
  const currentStreamingId = useRef<string | null>(null)
  const inFlightRef = useRef(false)
  const streamingContentRef = useRef('')
  const finalizedRef = useRef(false)

  // Cleanup function
  useEffect(() => {
    return () => {
      chatStreamService.abortStream()
    }
  }, [])

  // Send message
  const sendMessage = useCallback(
    async (query: string, conversation_id: number, context?: ChatStreamRequest['context']) => {
      if (!query.trim() || inFlightRef.current) return

      // Set this synchronously before React schedules its state update.  This
      // closes the double-click and repeated-Enter race window.
      inFlightRef.current = true

      // Add user message
      const userMessage: ChatMessage = {
        role: 'user',
        content: query.trim()
      }

      setChatState((prev) => ({
        ...prev,
        messages: [...prev.messages, userMessage],
        isLoading: true,
        error: undefined
      }))

      // Clear previous streaming message
      setStreamingMessage(null)
      currentStreamingId.current = null
      streamingContentRef.current = ''
      finalizedRef.current = false

      const request: ChatStreamRequest = {
        query: query.trim(),
        conversation_id,
        context: {
          ...context,
          // The current query is sent in `query`; history contains only
          // already completed messages.
          chat_history: chatState.messages
        },
        session_id: chatState.sessionId
      }

      try {
        await chatStreamService.sendStreamMessage(request, handleStreamEvent, handleStreamError, handleStreamComplete)
      } catch (error) {
        handleStreamError(error as Error)
      } finally {
        inFlightRef.current = false
      }
    },
    [chatState.messages, chatState.isLoading, chatState.sessionId]
  )

  const finalizeStreamingContent = useCallback((failure?: string) => {
    if (finalizedRef.current) return
    finalizedRef.current = true

    const content = streamingContentRef.current
    streamingContentRef.current = ''
    currentStreamingId.current = null
    setStreamingMessage(null)

    setChatState((prev) => ({
      ...prev,
      messages: content.trim() ? [...prev.messages, { role: 'assistant', content }] : prev.messages,
      isLoading: false,
      currentStage: failure ? 'failed' : 'completed',
      progress: failure ? prev.progress : 1.0,
      ...(failure ? { error: failure } : {})
    }))
  }, [])

  // Handle stream events
  const handleStreamEvent = useCallback((event: StreamEvent) => {
    if (finalizedRef.current) return
    console.log('🎯 Handling stream event:', event.type, event)

    switch (event.type) {
      case 'session_start':
        if (event.session_id) {
          setChatState((prev) => ({
            ...prev,
            sessionId: event.session_id!,
            messageId: get(event, 'assistant_message_id', prev.messageId)
          }))
        }
        break

      case 'thinking':
      case 'running':
        console.log('🤔 Handling thinking/running event:', event.type, event.content)
        setChatState((prev) => ({
          ...prev,
          currentStage: event.stage,
          progress: event.progress
        }))

        // Update or create a streaming message to show the thinking process
        // but append instead of overwriting existing content
        setStreamingMessage((prev) => {
          if (prev && prev.stage === event.stage) {
            // If it's the same stage, update the content
            return {
              ...prev,
              content: event.content,
              progress: event.progress,
              timestamp: event.timestamp
            }
          } else {
            // Create a new thinking message
            return {
              id: 'thinking_' + Date.now(),
              role: 'assistant',
              content: event.content,
              isStreaming: true,
              stage: event.stage,
              progress: event.progress,
              timestamp: event.timestamp
            }
          }
        })
        break

      case 'stream_chunk':
        streamingContentRef.current += event.content
        if (!currentStreamingId.current) {
          currentStreamingId.current = 'stream_' + Date.now()
          setStreamingMessage({
            id: currentStreamingId.current,
            role: 'assistant',
            content: event.content,
            isStreaming: true,
            stage: event.stage,
            progress: event.progress,
            timestamp: event.timestamp
          })
        } else {
          setStreamingMessage((prev) => {
            if (prev) {
              return {
                ...prev,
                content: prev.content + event.content,
                progress: event.progress,
                timestamp: event.timestamp
              }
            }
            return null
          })
        }
        break

      case 'stream_complete':
        finalizeStreamingContent()
        break

      case 'completed':
        // Completed (non-streaming mode) - use content from event directly
        if (event.content && !streamingContentRef.current) {
          streamingContentRef.current = event.content
        }
        finalizeStreamingContent()
        break

      case 'fail':
      case 'error':
      case 'interrupted':
        finalizeStreamingContent(event.content || '回答失败')
        break

      case 'done':
        setChatState((prev) => ({
          ...prev,
          currentStage: event.stage,
          progress: event.progress
        }))
        break
    }
  }, [finalizeStreamingContent])

  // Handle stream error
  const handleStreamError = useCallback((error: Error) => {
    console.error('❌ Stream request error:', error)

    let errorMessage = error.message
    if (error.name === 'TypeError' && error.message.includes('fetch')) {
      errorMessage = 'Unable to connect to AI service, please check network connection and service status'
    } else if (error.name === 'AbortError') {
      errorMessage = 'Request has been cancelled'
    }

    finalizeStreamingContent(errorMessage)
  }, [finalizeStreamingContent])

  // Handle stream completion
  const handleStreamComplete = useCallback(() => {
    console.log('Stream completed')
    setChatState((prev) => ({
      ...prev,
      isLoading: false
    }))
  }, [])

  // Clear chat history
  const clearChat = useCallback(() => {
    chatStreamService.abortStream()
    inFlightRef.current = false
    setChatState({
      messages: [],
      isLoading: false,
      progress: 0,
      sessionId: chatStreamService.generateSessionId(),
      messageId: -1
    })
    setStreamingMessage(null)
    currentStreamingId.current = null
    streamingContentRef.current = ''
    finalizedRef.current = false
  }, [])

  // Stop the current streaming request
  const stopStreaming = useCallback(() => {
    // Keep text already received visible and save it as an assistant message
    // before closing the network stream.
    finalizeStreamingContent('回答已停止')
    chatStreamService.abortStream()
  }, [finalizeStreamingContent])

  return {
    ...chatState,
    streamingMessage,
    sendMessage,
    clearChat,
    stopStreaming,
    setChatState
  }
}

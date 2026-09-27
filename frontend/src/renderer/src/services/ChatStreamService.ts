// Copyright (c) 2025 Beijing Volcano Engine Technology Co., Ltd.
// SPDX-License-Identifier: Apache-2.0

import axiosInstance from './axiosConfig'

// Type definitions for the Chat Stream API
export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
}

export interface DocumentInfo {
  id?: string
  title?: string
  content?: string
  summary?: string
  tags?: string[]
}

export interface ContextItem {
  source:
    | 'document'
    | 'web_search'
    | 'agent_memory'
    | 'context_db'
    | 'chat_history'
    | 'processed'
    | 'entity'
    | 'unknown'
  content: string
  title?: string
  relevance_score: number
  timestamp: string
  metadata?: Record<string, any>
}

export interface ChatContext {
  chat_history?: ChatMessage[]
  selected_content?: string
  document_id?: string
  current_document?: DocumentInfo
  collected_contexts?: ContextItem[]
  [key: string]: any // Other custom fields
}

export interface ChatStreamRequest {
  query: string
  context?: ChatContext
  session_id?: string
  user_id?: string
  conversation_id: number
}

export type EventType =
  | 'session_start'
  | 'thinking'
  | 'running'
  | 'done'
  | 'fail'
  | 'completed'
  | 'stream_chunk'
  | 'stream_complete'
  | 'error'
  | 'interrupted'

export type WorkflowStage =
  | 'init'
  | 'intent_analysis'
  | 'context_gathering'
  | 'execution'
  | 'reflection'
  | 'completed'
  | 'failed'
  | 'next'

export type NodeType = 'intent' | 'context' | 'execute' | 'reflect'

export interface StreamEvent {
  type: EventType
  content: string
  stage?: WorkflowStage
  node?: NodeType
  progress: number
  timestamp: string
  session_id?: string
  metadata?: {
    [key: string]: any
  }
  assistant_message_id?: number
}

// Streaming chat service class
export class ChatStreamService {
  private abortController?: AbortController
  private inFlight = false

  // Send a streaming chat request
  async sendStreamMessage(
    request: ChatStreamRequest,
    onEvent: (event: StreamEvent) => void,
    onError?: (error: Error) => void,
    onComplete?: () => void
  ): Promise<void> {
    if (this.inFlight) {
      throw new Error('上一条回答尚未结束，请等待完成后再发送。')
    }

    this.abortController = new AbortController()
    this.inFlight = true
    let terminalEventReceived = false
    let readerStarted = false

    try {
      // Use the baseURL of axiosInstance to ensure consistent ports
      const baseUrl = axiosInstance.defaults.baseURL || 'http://127.0.0.1:1733'
      const response = await fetch(`${baseUrl}/api/agent/chat/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(request),
        signal: this.abortController.signal
      })

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`)
      }

      const reader = response.body?.getReader()
      if (!reader) {
        throw new Error('Response body is not readable')
      }
      readerStarted = true

      const decoder = new TextDecoder()
      let buffer = ''

      const handleLine = (line: string) => {
        const trimmedLine = line.trim()
        if (!trimmedLine || !trimmedLine.startsWith('data: ')) return

        try {
          const eventData = JSON.parse(trimmedLine.slice(6))
          if (['stream_complete', 'completed', 'fail', 'error', 'interrupted'].includes(eventData.type)) {
            terminalEventReceived = true
          }
          onEvent(eventData as StreamEvent)
        } catch (parseError) {
          console.warn('Failed to parse SSE data:', line, parseError)
        }
      }

      while (true) {
        const { done, value } = await reader.read()

        if (done) {
          break
        }

        buffer += decoder.decode(value, { stream: true })

        // Handle multi-line data
        const lines = buffer.split('\n')
        buffer = lines.pop() || '' // Keep the potentially incomplete last line

        for (const line of lines) {
          handleLine(line)
        }
      }

      // Flush a partial UTF-8 sequence and process the final line when the
      // server closes without a trailing newline.
      buffer += decoder.decode()
      if (buffer) handleLine(buffer)
      if (terminalEventReceived) {
        onComplete?.()
      } else {
        onError?.(new Error('回答连接意外中断'))
      }
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        onComplete?.()
        return
      }

      console.error('Stream request failed:', error)
      if (terminalEventReceived) {
        onComplete?.()
      } else {
        onError?.(readerStarted ? new Error('回答连接意外中断') : (error as Error))
      }
    } finally {
      this.inFlight = false
      this.abortController = undefined
    }
  }

  // Cancel the current streaming request
  abortStream(): void {
    if (this.abortController) {
      this.abortController.abort()
    }
  }

  // Generate a session ID
  generateSessionId(): string {
    return 'session_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9)
  }
}

// Export a singleton instance
export const chatStreamService = new ChatStreamService()

/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { MarkdownSegment, MarkdownLine, NoteMilestone, ParsedNote } from '../core/models/markdown.model';
import { CheckerResult } from '../core/models/stream.model';

/**
 * Split text into bold, code, and plain segments
 */
export function parseMarkdownSegments(text: string): MarkdownSegment[] {
  if (!text) return [];
  const parts = text.split(/(\*\*.*?\*\*|`.*?`)/g);
  return parts
    .filter(part => part !== '')
    .map(part => {
      if (part.startsWith('**') && part.endsWith('**')) {
        return {
          text: part.slice(2, -2),
          bold: true,
          code: false
        };
      } else if (part.startsWith('`') && part.endsWith('`')) {
        return {
          text: part.slice(1, -1),
          bold: false,
          code: true
        };
      } else {
        return {
          text: part,
          bold: false,
          code: false
        };
      }
    });
}

/**
 * Parse markdown note content into structured lines (checklists, headers, list items)
 */
export function parseNoteLines(content: string): MarkdownLine[] {
  if (!content) return [];
  return content.split('\n').map(line => {
    const trimmed = line.trim();
    const indent = line.length - line.trimStart().length;

    // Checkbox [x]
    if (trimmed.startsWith('- [x] ') || trimmed.startsWith('- [X] ')) {
      const rawText = trimmed.substring(6);
      return { type: 'checked', segments: parseMarkdownSegments(rawText), indent };
    }
    // Active checkbox [/]
    if (trimmed.startsWith('- [/] ')) {
      const rawText = trimmed.substring(6);
      return { type: 'progress', segments: parseMarkdownSegments(rawText), indent };
    }
    // Unchecked checkbox [ ]
    if (trimmed.startsWith('- [ ] ')) {
      const rawText = trimmed.substring(6);
      return { type: 'unchecked', segments: parseMarkdownSegments(rawText), indent };
    }
    // Check lines (- verify: / - assert: / - verify@end: / - assert@end:)
    const checkMatch = trimmed.match(/^[-*]\s*(verify|assert)(@end)?\s*:\s*(.*)$/i);
    if (checkMatch) {
      const kind = checkMatch[1].toLowerCase() as 'verify' | 'assert';
      const atEnd = Boolean(checkMatch[2]);
      const rawText = checkMatch[3].trim();
      return {
        type: 'verify',
        checkKind: kind,
        atEnd,
        segments: parseMarkdownSegments(rawText),
        indent
      };
    }
    // System findings (- finding:)
    const findingMatch = trimmed.match(/^[-*]\s*finding\s*:\s*(.*)$/i);
    if (findingMatch) {
      const rawText = findingMatch[1].trim();
      return {
        type: 'finding',
        checkKind: 'finding',
        segments: parseMarkdownSegments(rawText),
        indent
      };
    }
    // Regular list item
    if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
      const rawText = trimmed.substring(2);
      return { type: 'list-item', segments: parseMarkdownSegments(rawText), indent };
    }
    // Headers
    if (trimmed.startsWith('# ')) {
      const rawText = trimmed.substring(2);
      return { type: 'h1', segments: parseMarkdownSegments(rawText), indent };
    }
    if (trimmed.startsWith('## ')) {
      const rawText = trimmed.substring(3);
      return { type: 'h2', segments: parseMarkdownSegments(rawText), indent };
    }
    if (trimmed.startsWith('### ')) {
      const rawText = trimmed.substring(4);
      return { type: 'h3', segments: parseMarkdownSegments(rawText), indent };
    }
    // Default line
    return trimmed ? { type: 'text', segments: parseMarkdownSegments(line), indent } : { type: 'empty', segments: [], indent: 0 };
  });
}

/**
 * Group parsed markdown lines into title, milestones (checklists), and other lines
 */
export function parseNote(content: string): ParsedNote {
  const lines = parseNoteLines(content);
  const milestones: NoteMilestone[] = [];
  const otherLines: MarkdownLine[] = [];
  let title: string | null = null;
  let currentMilestone: (NoteMilestone & { _indent?: number }) | null = null;
  let milestoneCount = 0;

  for (const line of lines) {
    if (line.type === 'empty') {
      continue;
    }

    if ((line.type === 'h1' || line.type === 'h2') && !title) {
      title = line.segments.map(s => s.text).join('');
      continue;
    }

    const isChecklistItem = ['checked', 'progress', 'unchecked'].includes(line.type);

    if (isChecklistItem && (!currentMilestone || line.indent <= (currentMilestone._indent ?? 0))) {
      milestoneCount++;
      currentMilestone = {
        index: milestoneCount,
        type: line.type as 'checked' | 'progress' | 'unchecked',
        segments: line.segments,
        subSteps: [],
        checks: [],
        _indent: line.indent
      };
      milestones.push(currentMilestone);
    } else if (currentMilestone) {
      if (line.indent > (currentMilestone._indent ?? 0)) {
        if (line.type === 'verify' || line.type === 'assert' || line.type === 'finding') {
          currentMilestone.checks.push(line);
        } else {
          currentMilestone.subSteps.push(line);
        }
      } else {
        otherLines.push(line);
      }
    } else {
      otherLines.push(line);
    }
  }

  return {
    title,
    milestones,
    otherLines
  };
}

/**
 * Parse the Checker JSON response block
 */
export function extractCheckerResult(text: string): CheckerResult | null {
  if (!text) return null;
  let cleanText = text.trim();
  if (cleanText.includes('```json')) {
    const parts = cleanText.split('```json');
    if (parts.length > 1) {
      cleanText = parts[1].split('```')[0].trim();
    }
  } else if (cleanText.includes('```')) {
    const parts = cleanText.split('```');
    if (parts.length > 1) {
      cleanText = parts[1].split('```')[0].trim();
    }
  }

  try {
    const parsed = JSON.parse(cleanText);
    if (parsed && typeof parsed === 'object' && 'success' in parsed) {
      return {
        success: Boolean(parsed.success),
        reason: parsed.reason || 'No reason provided.'
      };
    }
  } catch {
    // Ignore
  }
  return null;
}

/**
 * Helper to format inline markdown segments (bold, italic, code, strikethrough)
 */
function formatInlineMarkdown(text: string): string {
  if (!text) return '';
  return text
    // Inline code first (so asterisks inside code aren't formatted)
    .replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')
    // Bold + Italic
    .replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/___([^_]+)___/g, '<strong><em>$1</em></strong>')
    // Bold
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    // Italic
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\b_([^_]+)_\b/g, '<em>$1</em>')
    // Strikethrough
    .replace(/~~([^~]+)~~/g, '<del>$1</del>');
}

/**
 * Convert basic markdown text to safe HTML for agent logs/thinking
 */
export function renderMarkdownToHtml(text: string): string {
  if (!text) return '';

  // Clean up internal agent XML-like tags (e.g. <thought>, <short_term_memory>, etc.)
  const cleanedText = text
    .replace(/<\/?(thought|short_term_memory|strong_term_memory|reasoning|plan|task_plan|call_tool)[^\n>]*>?/gi, '')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  // Escape HTML first to prevent XSS
  const escaped = cleanedText
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  const rawLines = escaped.split('\n');
  const result: string[] = [];
  let currentListType: 'ul' | 'ol' | null = null;
  let inCodeBlock = false;
  let codeBlockLang = '';
  let codeBlockLines: string[] = [];

  const closeList = () => {
    if (currentListType) {
      result.push(`</${currentListType}>`);
      currentListType = null;
    }
  };

  for (let i = 0; i < rawLines.length; i++) {
    const rawLine = rawLines[i];
    const trimmed = rawLine.trim();

    // 1. Code blocks: ```lang ... ```
    if (trimmed.startsWith('```')) {
      if (inCodeBlock) {
        // End code block
        const codeContent = codeBlockLines.join('\n');
        result.push(`<pre class="code-block"><code class="lang-${codeBlockLang}">${codeContent}</code></pre>`);
        inCodeBlock = false;
        codeBlockLines = [];
        codeBlockLang = '';
      } else {
        closeList();
        inCodeBlock = true;
        codeBlockLang = trimmed.substring(3).trim();
        codeBlockLines = [];
      }
      continue;
    }

    if (inCodeBlock) {
      codeBlockLines.push(rawLine);
      continue;
    }

    // 2. Headings: # to ######
    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      closeList();
      const level = headingMatch[1].length;
      const headingContent = formatInlineMarkdown(headingMatch[2]);
      result.push(`<h${level}>${headingContent}</h${level}>`);
      continue;
    }

    // 3. Horizontal Rule: ---, ***, ___
    if (/^(\-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      closeList();
      result.push('<hr class="md-hr" />');
      continue;
    }

    // 4. Blockquotes: > quote
    if (trimmed.startsWith('&gt; ') || trimmed.startsWith('&gt;')) {
      closeList();
      const quoteContent = formatInlineMarkdown(trimmed.replace(/^&gt;\s?/, ''));
      result.push(`<blockquote>${quoteContent}</blockquote>`);
      continue;
    }

    // 5. Unordered List: - item or * item
    const ulMatch = rawLine.match(/^(\s*)([\*\-])\s+(.*)$/);
    if (ulMatch) {
      if (currentListType !== 'ul') {
        closeList();
        currentListType = 'ul';
        result.push('<ul>');
      }
      const spaces = ulMatch[1].length;
      const subLevel = Math.max(0, Math.floor((spaces - 2) / 2));
      const indent = subLevel * 12;
      const style = indent ? ` style="margin-left: ${indent}px"` : '';
      const itemText = ulMatch[3];
      const checkMatch = itemText.match(/^(verify|assert)(@end)?\s*:\s*(.*)$/i);
      const findingMatch = itemText.match(/^finding\s*:\s*(.*)$/i);
      if (checkMatch) {
        const kind = checkMatch[1].toLowerCase();
        const atEnd = checkMatch[2] ? '@end' : '';
        const assertClass = kind === 'assert' ? ' assert' : '';
        const itemContent = formatInlineMarkdown(checkMatch[3].trim());
        result.push(`<li${style} class="md-verify-item"><span class="verify-badge${assertClass}">${kind}${atEnd}</span>${itemContent}</li>`);
      } else if (findingMatch) {
        const itemContent = formatInlineMarkdown(findingMatch[1].trim());
        result.push(`<li${style} class="md-finding-item"><span class="finding-badge">finding</span>${itemContent}</li>`);
      } else {
        const itemContent = formatInlineMarkdown(itemText);
        result.push(`<li${style}>${itemContent}</li>`);
      }
      continue;
    }

    // 6. Ordered List: 1. item
    const olMatch = rawLine.match(/^(\s*)(\d+)\.\s+(.*)$/);
    if (olMatch) {
      if (currentListType !== 'ol') {
        closeList();
        currentListType = 'ol';
        result.push('<ol>');
      }
      const spaces = olMatch[1].length;
      const subLevel = Math.max(0, Math.floor((spaces - 2) / 2));
      const indent = subLevel * 12;
      const style = indent ? ` style="margin-left: ${indent}px"` : '';
      const itemContent = formatInlineMarkdown(olMatch[3]);
      result.push(`<li${style}>${itemContent}</li>`);
      continue;
    }

    // 7. Regular paragraph / empty line
    closeList();
    if (trimmed === '') {
      result.push('<div class="md-para-gap"></div>');
    } else {
      const lineContent = formatInlineMarkdown(rawLine);
      result.push(`<div>${lineContent}</div>`);
    }
  }

  if (inCodeBlock) {
    const codeContent = codeBlockLines.join('\n');
    result.push(`<pre class="code-block"><code class="lang-${codeBlockLang}">${codeContent}</code></pre>`);
  }

  closeList();
  return result.join('\n');
}

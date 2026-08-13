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
        _indent: line.indent
      };
      milestones.push(currentMilestone);
    } else if (currentMilestone) {
      if (line.indent > (currentMilestone._indent ?? 0)) {
        currentMilestone.subSteps.push(line);
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
 * Convert basic markdown text to safe HTML for agent logs/thinking
 */
export function renderMarkdownToHtml(text: string): string {
  if (!text) return '';

  // Clean up internal agent XML-like tags (e.g. <thought>, <short_term_memory>, etc.)
  const cleanedText = text
    .replace(/<\/?(thought|short_term_memory|strong_term_memory|reasoning|plan|task_plan|call_tool)[^\n>]*>?/gi, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  // 1. Escape HTML to prevent XSS
  let html = cleanedText
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');

  // 2. Bold: **text** -> <strong>text</strong>
  html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');

  // 3. Inline code: `code` -> <code class="inline-code">code</code>
  html = html.replace(/`(.*?)`/g, '<code class="inline-code">$1</code>');

  // 4. Lines processing for lists
  const lines = html.split('\n');
  let output = '';
  let currentListType: 'ul' | 'ol' | null = null;

  for (const line of lines) {
    const trimmed = line.trim();
    const ulMatch = line.match(/^(\s*)([\*\-])\s+(.*)$/);
    const olMatch = line.match(/^(\s*)(\d+)\.\s+(.*)$/);

    if (ulMatch) {
      if (currentListType !== 'ul') {
        if (currentListType) output += `</${currentListType}>\n`;
        currentListType = 'ul';
        output += '<ul>\n';
      }
      const spaces = ulMatch[1].length;
      const subLevel = Math.max(0, Math.floor((spaces - 2) / 2));
      const indent = subLevel * 12;
      const style = indent ? ` style="margin-left: ${indent}px"` : '';
      output += `<li${style}>${ulMatch[3]}</li>\n`;
    } else if (olMatch) {
      if (currentListType !== 'ol') {
        if (currentListType) output += `</${currentListType}>\n`;
        currentListType = 'ol';
        output += '<ol>\n';
      }
      const spaces = olMatch[1].length;
      const subLevel = Math.max(0, Math.floor((spaces - 2) / 2));
      const indent = subLevel * 12;
      const style = indent ? ` style="margin-left: ${indent}px"` : '';
      output += `<li${style}>${olMatch[3]}</li>\n`;
    } else {
      if (currentListType) {
        output += `</${currentListType}>\n`;
        currentListType = null;
      }
      if (trimmed === '') {
        output += '<div class="md-para-gap"></div>';
      } else {
        output += `<div>${line}</div>\n`;
      }
    }
  }

  if (currentListType) {
    output += `</${currentListType}>\n`;
  }

  return output;
}

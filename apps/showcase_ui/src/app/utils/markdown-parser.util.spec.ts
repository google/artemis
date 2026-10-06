import { parseNoteLines, parseNote, renderMarkdownToHtml } from './markdown-parser.util';

describe('markdown-parser.util verification & check lines', () => {
  it('should parse - verify: line as type verify with checkKind verify', () => {
    const markdown = `- [x] Parent task
  - [x] Subtask 1
  - verify: Commute duration is recorded in note \`eta_details\``;

    const lines = parseNoteLines(markdown);
    expect(lines.length).toBe(3);
    expect(lines[0].type).toBe('checked');
    expect(lines[1].type).toBe('checked');

    const verifyLine = lines[2];
    expect(verifyLine.type).toBe('verify');
    expect(verifyLine.checkKind).toBe('verify');
    expect(verifyLine.atEnd).toBe(false);
    expect(verifyLine.segments.map(s => s.text).join('')).toBe('Commute duration is recorded in note eta_details');
    expect(verifyLine.segments.find(s => s.code)?.text).toBe('eta_details');
  });

  it('should parse - assert: and - assert@end: lines', () => {
    const markdown = `- [ ] Open app
  - assert: the welcome screen shows up
- assert@end: final status is completed`;

    const lines = parseNoteLines(markdown);
    expect(lines.length).toBe(3);

    expect(lines[1].type).toBe('verify');
    expect(lines[1].checkKind).toBe('assert');
    expect(lines[1].atEnd).toBe(false);

    expect(lines[2].type).toBe('verify');
    expect(lines[2].checkKind).toBe('assert');
    expect(lines[2].atEnd).toBe(true);
  });

  it('should parse - finding: lines', () => {
    const markdown = `- [x] Step 1
  - finding: Unresolved verify failure`;

    const lines = parseNoteLines(markdown);
    expect(lines[1].type).toBe('finding');
    expect(lines[1].checkKind).toBe('finding');
    expect(lines[1].segments[0].text).toBe('Unresolved verify failure');
  });

  it('should group verify lines under parent milestone checks in parseNote', () => {
    const note = `- [x] Open Maps
  - [x] Search destination
  - verify: Arrival time is visible in note \`eta\``;

    const parsed = parseNote(note);
    expect(parsed.milestones.length).toBe(1);
    expect(parsed.milestones[0].subSteps.length).toBe(1);
    expect(parsed.milestones[0].subSteps[0].type).toBe('checked');
    expect(parsed.milestones[0].checks.length).toBe(1);
    expect(parsed.milestones[0].checks[0].type).toBe('verify');
    expect(parsed.milestones[0].checks[0].checkKind).toBe('verify');
  });

  it('should render verify badges in renderMarkdownToHtml', () => {
    const text = `- verify: status is ok`;
    const html = renderMarkdownToHtml(text);
    expect(html).toContain('class="md-verify-item"');
    expect(html).toContain('<span class="verify-badge">verify</span>');
    expect(html).toContain('status is ok');
  });
});

describe('renderMarkdownToHtml inline code', () => {
  for (const literal of ['**literal**', '__name__', 'a*b*c', '_literal_', '***literal***', '___literal___', '~~literal~~']) {
    it(`should preserve ${literal} inside inline code`, () => {
      expect(renderMarkdownToHtml('`' + literal + '`'))
        .toBe(`<div><code class="inline-code">${literal}</code></div>`);
    });
  }

  it('should format surrounding text while preserving multiple code spans', () => {
    expect(renderMarkdownToHtml('**bold** `**first**` _italic_ `__second__` ~~removed~~'))
      .toBe('<div><strong>bold</strong> <code class="inline-code">**first**</code> <em>italic</em> <code class="inline-code">__second__</code> <del>removed</del></div>');
  });

  for (const [delimiter, open, close] of [
    ['***', '<strong><em>', '</em></strong>'],
    ['___', '<strong><em>', '</em></strong>'],
    ['**', '<strong>', '</strong>'],
    ['__', '<strong>', '</strong>'],
    ['*', '<em>', '</em>'],
    ['_', '<em>', '</em>'],
    ['~~', '<del>', '</del>'],
  ]) {
    it(`should preserve ${delimiter} formatting around inline code`, () => {
      expect(renderMarkdownToHtml(delimiter + 'before `a*b_c~d` after' + delimiter))
        .toBe(`<div>${open}before <code class="inline-code">a*b_c~d</code> after${close}</div>`);
    });
  }

  it('should not pair formatting delimiters across code spans', () => {
    expect(renderMarkdownToHtml('`a*` plain `*b` and `a_` plain `_b` and `a~~` plain `~~b`'))
      .toBe('<div><code class="inline-code">a*</code> plain <code class="inline-code">*b</code> and <code class="inline-code">a_</code> plain <code class="inline-code">_b</code> and <code class="inline-code">a~~</code> plain <code class="inline-code">~~b</code></div>');
  });

  it('should handle adjacent code spans', () => {
    expect(renderMarkdownToHtml('`**first**``__second__`'))
      .toBe('<div><code class="inline-code">**first**</code><code class="inline-code">__second__</code></div>');
  });

  it('should keep HTML escaped inside and outside inline code', () => {
    expect(renderMarkdownToHtml('<b>outside</b> `<img src=x onerror="alert(1)"> & </code> **literal**`'))
      .toBe('<div>&lt;b&gt;outside&lt;/b&gt; <code class="inline-code">&lt;img src=x onerror="alert(1)"&gt; &amp; &lt;/code&gt; **literal**</code></div>');
  });

  it('should leave empty and unmatched backticks unchanged', () => {
    expect(renderMarkdownToHtml('``')).toBe('<div>``</div>');
    expect(renderMarkdownToHtml('`unfinished **bold**'))
      .toBe('<div>`unfinished <strong>bold</strong></div>');
  });

  it('should preserve underscore word boundaries next to code', () => {
    expect(renderMarkdownToHtml('`code`_after_ and _before_`code` and _`code`_'))
      .toBe('<div><code class="inline-code">code</code><em>after</em> and <em>before</em><code class="inline-code">code</code> and <em><code class="inline-code">code</code></em></div>');
  });

  it('should not pair outside delimiters with code contents', () => {
    expect(renderMarkdownToHtml('*before `x*` and `*x` after*'))
      .toBe('<div><em>before <code class="inline-code">x*</code> and <code class="inline-code">*x</code> after</em></div>');
  });

  it('should preserve placeholder-like text and replacement patterns literally', () => {
    expect(renderMarkdownToHtml('<inline-code-0> `<inline-code-1> $& $1 $$ **literal**`'))
      .toBe('<div>&lt;inline-code-0&gt; <code class="inline-code">&lt;inline-code-1&gt; $&amp; $1 $$ **literal**</code></div>');
  });

  it('should protect code in headings, lists, and blockquotes', () => {
    expect(renderMarkdownToHtml('# `**heading**`\n- `__item__`\n> `~~quote~~`'))
      .toBe('<h1><code class="inline-code">**heading**</code></h1>\n<ul>\n<li><code class="inline-code">__item__</code></li>\n</ul>\n<blockquote><code class="inline-code">~~quote~~</code></blockquote>');
  });

  it('should leave fenced code formatting unchanged', () => {
    expect(renderMarkdownToHtml('```text\n`**literal**` <b>&</b>\n```'))
      .toBe('<pre class="code-block"><code class="lang-text">`**literal**` &lt;b&gt;&amp;&lt;/b&gt;</code></pre>');
  });
});

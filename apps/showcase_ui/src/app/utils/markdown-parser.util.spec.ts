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

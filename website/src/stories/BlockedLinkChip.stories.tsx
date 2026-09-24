import type { Meta, StoryObj } from '@storybook/react-vite'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * The chip that replaces a blocked link's bare placeholder.
 *
 * The saved text keeps only the site name; the chip reads its detail, and the
 * full address it can open, from the records the redactor kept in the message's
 * meta. These stories drive the renderer with the same `blockedLinks` shape the
 * dashboard passes from `meta.blocked_links`. Click *See link* to open a card.
 */
const PLACEHOLDER = (domain: string) => `[REDACTED: suspicious URL to ${domain}]`

type Rec = {
  domain: string
  rule: string
  path: string | null
  query_chars: number
  url: string | null
  url_withheld: 'credential' | 'length' | null
}
const kept = (domain: string, rule: string, path: string | null, url: string): Rec => {
  const q = url.indexOf('?')
  return { domain, rule, path, query_chars: q === -1 ? 0 : url.length - q - 1, url, url_withheld: null }
}
const withheld = (domain: string, rule: string, path: string | null, query_chars: number): Rec =>
  ({ domain, rule, path, query_chars, url: null, url_withheld: 'credential' })

const REVIEWS_URL =
  'https://reviewers.security.example.dev/reviews?status=open&team=platform-observability'
  + '&assignee=me&sort=updated&order=desc&columns=title,author,updated,labels,state'
  + '&labels=needs-review,security,redaction&since=2026-09-01&until=2026-09-30&page=1&per_page=50'
const WIKI_URL =
  'https://wiki.example.com/view/%E5%8F%91%E5%B8%83%E6%B5%81%E7%A8%8B%E4%B8%8E%E5%9B%9E%E6%BB%9A%E6%89%8B%E5%86%8C'

const CDN_QUERY_PAIRS = 60
const CDN_URL = `https://cdn.example.io/a?${Array.from({ length: CDN_QUERY_PAIRS }, () => 'v=1').join('&')}`

const meta = {
  title: 'Chat/BlockedLinkChip',
  component: MarkdownRenderer,
  parameters: { layout: 'padded' },
} satisfies Meta<typeof MarkdownRenderer>

export default meta
type Story = StoryObj<typeof meta>

/** The common false positive: an ordinary long query. See link gives it back. */
export const FullDetail: Story = {
  args: {
    content: `Here is the reviewers filter page: ${PLACEHOLDER('reviewers.security.example.dev')}`,
    blockedLinks: [kept('reviewers.security.example.dev', 'exfil_query_length', '/reviews', REVIEWS_URL)],
  },
}

/** A page title in Chinese percent-encodes into the shape the encoding rule matches. */
export const NonLatinTitle: Story = {
  args: {
    content: `The rollback runbook: ${PLACEHOLDER('wiki.example.com')}`,
    blockedLinks: [kept('wiki.example.com', 'exfil_percent_encoding', null, WIKI_URL)],
  },
}

/** Opening this would send a secret, so the address was never kept and there is no Open. */
export const CredentialWithheld: Story = {
  args: {
    content: `The callback it built: ${PLACEHOLDER('auth.example.org')}`,
    blockedLinks: [withheld('auth.example.org', 'exfil_hard_credential', '/callback', 64)],
  },
}

/** An address longer than the limit is withheld rather than truncated. */
export const LengthWithheld: Story = {
  args: {
    content: `The export it offered: ${PLACEHOLDER('artifacts.example.org')}`,
    blockedLinks: [{ ...withheld('artifacts.example.org', 'exfil_query_length', '/export', 9400), url_withheld: 'length' as const }],
  },
}

/**
 * Two different links on one site redact to identical placeholders. The card
 * lists both, each with its own address and actions, instead of guessing.
 */
export const TwoLinksOneSite: Story = {
  args: {
    content: `First ${PLACEHOLDER('cdn.example.io')} and second ${PLACEHOLDER('cdn.example.io')}`,
    blockedLinks: [
      kept('cdn.example.io', 'exfil_query_length', '/a', CDN_URL),
      withheld('cdn.example.io', 'exfil_hard_credential', '/b', 980),
    ],
  },
}

/** The chip sits in flowing prose, so the paragraph keeps wrapping around it. */
export const InFlowingProse: Story = {
  args: {
    content:
      'The build page it pointed at is the one we were looking at earlier, and the link '
      + `came through as ${PLACEHOLDER('wiki.example.com')} rather than as an anchor, so the `
      + 'rest of this sentence has to keep reading normally around it and wrap the way any '
      + 'other inline element would.',
    blockedLinks: [kept('wiki.example.com', 'exfil_percent_encoding', null, WIKI_URL)],
  },
}

/** A transcript written before the records existed keeps its plain text. */
export const NoRecordFallback: Story = {
  args: {
    content: `An older message: ${PLACEHOLDER('legacy.example.com')}`,
    blockedLinks: [],
  },
}

/**
 * Both gates in one message: the dashed warning border reads as a different
 * kind of thing from the solid remote-media chip beside it.
 */
export const BesideTheRemoteMediaChip: Story = {
  args: {
    content:
      `The chart it linked to: ${PLACEHOLDER('reviewers.security.example.dev')}\n\n`
      + 'And the chart itself:\n\n'
      + '<img src="https://metrics.example.com/weekly.png" alt="Weekly traffic chart">',
    blockedLinks: [kept('reviewers.security.example.dev', 'exfil_query_length', '/reviews', REVIEWS_URL)],
  },
}

/** The two reason sentences the other stories do not reach. */
export const OtherReasons: Story = {
  args: {
    content:
      `Shaped like data: ${PLACEHOLDER('metrics.example.com')}\n\n`
      + `Another rule: ${PLACEHOLDER('status.example.net')}`,
    blockedLinks: [
      kept('metrics.example.com', 'exfil_query_pattern', '/q', 'https://metrics.example.com/q?payload=ZXhhbXBsZSBkYXRh'),
      kept('status.example.net', 'exfil_unrecognised', '/s', 'https://status.example.net/s?id=1'),
    ],
  },
}

/** Every state in one frame. */
export const AllStates: Story = {
  args: { content: '' },
  render: () => (
    <div className="flex flex-col gap-6">
      {[
        ['A long query — the link is kept and can be opened', FullDetail.args],
        ['A non-Latin page title', NonLatinTitle.args],
        ['Would send a credential — not kept, no Open', CredentialWithheld.args],
        ['Too long to keep — not kept, no Open', LengthWithheld.args],
        ['Two links on one site', TwoLinksOneSite.args],
        ['In flowing prose', InFlowingProse.args],
        ['No record — the text stays as it reads today', NoRecordFallback.args],
        ['Beside the remote-media chip', BesideTheRemoteMediaChip.args],
      ].map(([label, args]) => (
        <div key={label as string} className="flex flex-col gap-1.5">
          <span className="text-[11px] uppercase tracking-wide text-muted">{label as string}</span>
          <div className="rounded-md border border-border p-3">
            <MarkdownRenderer {...(args as { content: string; blockedLinks?: unknown })} />
          </div>
        </div>
      ))}
    </div>
  ),
}

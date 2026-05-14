# HTML action API

HTML messages run in a sandboxed iframe with `allow-scripts allow-forms`, but
without `allow-same-origin`. Scripts can run, forms can submit, and the frame has
an opaque origin. The reply cannot read the parent DOM or act as same-origin app
code.

The harness injects a tiny bridge into every HTML message. Use
`data-strix-action` for declarative controls, or `window.strix` / `postMessage`
from script. The parent owns the actual state change.

---

# Actions

## `widget.navigate`

Navigate a running UI plugin widget.

Required:

- `data-strix-action="widget.navigate"`
- `data-strix-widget="<plugin-name>"`

Optional:

- `data-strix-path="<plugin-route>"`
- `href="/ui/<plugin-name>/<plugin-route>"`

`data-strix-path` may be plugin-local (`/issue/567`, `issue/567`) or already in
canonical chat-link form (`/ui/chainlink/issue/567`). The harness normalizes it
to `/ui/<plugin>/<path>`, un-minimizes the widget, scrolls it into view, and sets
the widget iframe `src`.

```html
<a
  href="/ui/chainlink/issue/567"
  data-strix-action="widget.navigate"
  data-strix-widget="chainlink"
>
  Open issue 567
</a>

<button
  type="button"
  data-strix-action="widget.navigate"
  data-strix-widget="chainlink"
  data-strix-path="/issue/567"
>
  Open issue 567
</button>
```

Plain links still work too:

```html
<a href="/ui/chainlink/issue/567">Open issue 567</a>
```

Use explicit `data-strix-action` when the element is not a normal link, when the
path is plugin-local, or when you want the intent to be obvious in generated
HTML.

## `chat.send`

Send a user message into the local web chat.

For a link or button, provide `data-strix-message`:

```html
<button
  type="button"
  data-strix-action="chat.send"
  data-strix-message="Summarize chainlink issue 567 and suggest the next step."
>
  Ask for summary
</button>
```

For a form, put the action on the form and include a field named `message`,
`text`, or `prompt`:

```html
<form data-strix-action="chat.send">
  <input
    name="message"
    value="Compare the open chainlink issues by impact and effort."
  >
  <button type="submit">Ask</button>
</form>
```

File inputs are forwarded as attachments when present:

```html
<form data-strix-action="chat.send">
  <textarea name="message">Review this screenshot.</textarea>
  <input type="file" name="files">
  <button type="submit">Send</button>
</form>
```

Do not use `action="/api/messages"` on these forms. The parent intercepts the
submit, validates the action, posts to the chat API, and refreshes the message
list.

---

# JavaScript bridge

HTML messages get a `window.strix` helper:

```js
window.strix.navigateWidget("chainlink", "/issue/567");
window.strix.sendMessage("Summarize chainlink issue 567 and suggest the next step.");
window.strix.resize();
```

You can also call the same actions with `postMessage`. HTML messages have an
opaque origin, so use `"*"` as the target origin; the parent validates the source
iframe before doing anything.

```js
window.parent.postMessage(
  {
    strix: "v1",
    action: "widget.navigate",
    widget: "chainlink",
    path: "/issue/567",
  },
  "*",
);
```

```js
window.parent.postMessage(
  {
    strix: "v1",
    action: "chat.send",
    message: "Summarize chainlink issue 567 and suggest the next step.",
  },
  "*",
);
```

UI plugin frames are same-origin trusted app surfaces and can use
`window.location.origin` as the target origin if they prefer.

The bridge is fire-and-forget in v1. If a plugin needs request/response
semantics later, add an explicit `requestId` protocol rather than inferring
success from navigation or chat refresh side effects.

---

# Security model

- HTML message scripts run, but without `allow-same-origin`.
- The injected bridge is a capability API. The parent decides which actions
  exist and how they mutate state.
- Unknown `data-strix-action` values are ignored.
- `postMessage` actions are accepted from same-origin trusted frames or from
  HTML message iframes the parent created.
- `widget.navigate` only claims known UI plugin widgets.
- `chat.send` goes through the same `/api/messages` path as the composer.

Keep the action vocabulary small. Add a new action when it represents a durable
app-level capability, not for one-off DOM manipulation.

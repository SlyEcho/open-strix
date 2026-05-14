# HTML action API

HTML messages are static: the iframe sandbox does not allow scripts. They can
still ask the parent web UI to perform a small set of controlled actions by
using `data-strix-action` attributes. The parent owns the actual state change.

UI plugin frames can use the same verbs from JavaScript with `postMessage`.
That is the scripted twin of the declarative HTML API.

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

UI plugins and other trusted scripted frames can call the same actions with
`window.parent.postMessage`. The message must be same-origin and include
`strix: "v1"`.

```js
window.parent.postMessage(
  {
    strix: "v1",
    action: "widget.navigate",
    widget: "chainlink",
    path: "/issue/567",
  },
  window.location.origin,
);
```

```js
window.parent.postMessage(
  {
    strix: "v1",
    action: "chat.send",
    message: "Summarize chainlink issue 567 and suggest the next step.",
  },
  window.location.origin,
);
```

The bridge is fire-and-forget in v1. If a plugin needs request/response
semantics later, add an explicit `requestId` protocol rather than inferring
success from navigation or chat refresh side effects.

---

# Security model

- HTML messages still do not run scripts.
- The parent web UI decides which actions exist and how they mutate state.
- Unknown `data-strix-action` values are ignored.
- `postMessage` actions are accepted only from the same origin.
- `widget.navigate` only claims known UI plugin widgets.
- `chat.send` goes through the same `/api/messages` path as the composer.

Keep the action vocabulary small. Add a new action when it represents a durable
app-level capability, not for one-off DOM manipulation.

"""Read submitted fields from the same HTML the browser receives."""

from html.parser import HTMLParser


class RenderedForms(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.forms = []
        self.current = None
        self.button = None
        self.select = None
        self.option = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action"), "fields": {}, "buttons": [], "selects": {}}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            if ("disabled" not in attrs and attrs.get("type") not in ("radio", "checkbox")
                    and (name := attrs.get("name"))):
                self.current["fields"][name] = attrs.get("value", "")
        elif tag == "button" and self.current is not None:
            self.button = {"name": attrs.get("name"), "value": attrs.get("value", ""), "text": ""}
            self.current["buttons"].append(self.button)
        elif tag == "select" and self.current is not None:
            self.select = attrs.get("name")
            self.current["selects"][self.select] = []
        elif tag == "option" and self.select is not None:
            self.option = {"value": attrs.get("value", ""), "text": ""}
            self.current["selects"][self.select].append(self.option)
            if self.select not in self.current["fields"] or "selected" in attrs:
                self.current["fields"][self.select] = self.option["value"]

    def handle_data(self, data):
        if self.button is not None:
            self.button["text"] += data
        if self.option is not None:
            self.option["text"] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None
        elif tag == "button":
            self.button = None
        elif tag == "select":
            self.select = None
        elif tag == "option":
            self.option = None

    def with_button(self, caption):
        for form in self.forms:
            for button in form["buttons"]:
                if caption in button["text"]:
                    fields = dict(form["fields"])
                    if button["name"]:
                        fields[button["name"]] = button["value"]
                    return {"action": form["action"], "fields": fields, "selects": form["selects"]}
        raise AssertionError(f"no form with button {caption!r}")

#!/usr/bin/env python3
"""Internal JSON transport; runs as the desktop user, never evaluates shell text."""
import json
import sys
from gi.repository import Gio, GLib


def main(request):
    if request["action"] == "restart-panel":
        import subprocess
        subprocess.run(["xfce4-panel", "-r"], timeout=10)
        return None
    if request["action"] == "normalize":
        value = decode(request["value"])
        if request["key"][0] == "xf" and value.get_type_string().startswith("a") and value.get_type_string() != "av":
            value = GLib.Variant("av", [value.get_child_value(n) for n in range(value.n_children())])
        return dict(type=value.get_type_string(), text=value.print_(True))
    kind, namespace, prop = request["key"]
    action = request["action"]
    if kind == "gs":
        source = Gio.SettingsSchemaSource.get_default()
        schema = source.lookup(namespace, True) if source else None
        if schema is None or not schema.has_key(prop):
            raise ValueError("Missing GSettings schema/key: " + namespace + " " + prop)
        settings = Gio.Settings.new(namespace)
        if action == "get":
            value = settings.get_user_value(prop)
        else:
            value = request["value"]
            if not settings.set_value(prop, decode(value)):
                raise ValueError("Setting rejected")
            Gio.Settings.sync()
            return None
    else:
        proxy = Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE,
                    None, "org.xfce.Xfconf", "/org/xfce/Xfconf", "org.xfce.Xfconf", None)
        def call(method, signature, values):
            return proxy.call_sync(method, GLib.Variant(signature, values),
                                   Gio.DBusCallFlags.NONE, 10000, None)
        if action == "list":
            try:
                return list(call("GetAllProperties", "(ss)", (namespace, prop)).unpack()[0])
            except GLib.Error as error:
                if (Gio.DBusError.get_remote_error(error) or "").endswith(".PropertyNotFound"):
                    return []
                raise
        if action == "get":
            exists = call("PropertyExists", "(ss)", (namespace, prop)).unpack()[0]
            if not exists:
                return None
            value = call("GetProperty", "(ss)", (namespace, prop)).get_child_value(0).get_variant()
        else:
            value = request["value"]
            call("SetProperty", "(ssv)", (namespace, prop, decode(value)))
            return None
    if value is None:
        return None
    unpacked = value.unpack()
    result = dict(type=value.get_type_string(), text=value.print_(True))
    if not isinstance(unpacked, (bytes, bytearray)):
        result["value"] = unpacked
    return result


def decode(value):
    return GLib.Variant.parse(GLib.VariantType.new(value["type"]), value["text"], None, None)


if __name__ == "__main__":
    try:
        print(json.dumps(main(json.load(sys.stdin))))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

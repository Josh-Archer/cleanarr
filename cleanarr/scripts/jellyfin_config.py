import json
import os
import sys
import xml.etree.ElementTree as ET

def configure_jellyfin_webhook():
    config_dir = "/config/plugins/configurations"
    json_path = os.path.join(config_dir, "Jellyfin.Plugin.Webhooks.json")
    xml_path = os.path.join(config_dir, "Jellyfin.Plugin.Webhook.xml")
    
    webhook_url = os.environ.get("JELLYFIN_WEBHOOK_URL", "http://cleanarr-webhook-proxy:8000/jellyfin/webhook")
    token = os.environ.get("JELLYFIN_WEBHOOK_SECRET", "")

    if not token:
        print("Error: JELLYFIN_WEBHOOK_SECRET environment variable is not set.")
        sys.exit(1)

    os.makedirs(config_dir, exist_ok=True)

    # --- 1. Update JSON (Legacy/Specific version support) ---
    new_webhook_json = {
        "Url": webhook_url,
        "Name": "Cleanarr Webhook",
        "Enable": True,
        "RequestContentType": "application/json",
        "AddHeaders": [
            {
                "Key": "X-Cleanarr-Webhook-Token",
                "Value": token
            }
        ],
        "NotificationTypes": [
            "ItemMarkPlayed",
            "PlaybackStart",
            "PlaybackStopped"
        ],
        "ItemId": None,
        "UserId": None
    }

    data = []
    if os.path.exists(json_path):
        with open(json_path, "r") as f:
            try:
                data = json.load(f)
            except Exception:
                data = []

    if not isinstance(data, list):
        data = []

    found = False
    for i, item in enumerate(data):
        if isinstance(item, dict) and item.get("Url") == webhook_url:
            data[i]["AddHeaders"] = new_webhook_json["AddHeaders"]
            data[i]["NotificationTypes"] = list(set(item.get("NotificationTypes", []) + new_webhook_json["NotificationTypes"]))
            data[i]["Name"] = new_webhook_json["Name"]
            data[i]["Enable"] = True
            found = True
            break

    if not found:
        data.append(new_webhook_json)

    with open(json_path, "w") as f:
        json.dump(data, f, indent=4)
    print(f"Successfully configured Jellyfin webhook JSON at {json_path}")

    # --- 2. Update XML (Official v18 support) ---
    template = """{
  "NotificationType": "{{NotificationType}}",
  "NotificationUsername": "{{NotificationUsername}}",
  "UserId": "{{UserId}}",
  "ItemType": "{{ItemType}}",
  "Name": "{{Name}}"
}"""

    try:
        if os.path.exists(xml_path):
            try:
                tree = ET.parse(xml_path)
                root = tree.getroot()
            except Exception:
                root = ET.Element("PluginConfiguration")
        else:
            root = ET.Element("PluginConfiguration")

        destinations = root.find("Destinations")
        if destinations is None:
            destinations = ET.SubElement(root, "Destinations")

        existing = False
        for dest in destinations.findall("GenericDestination"):
            name_el = dest.find("Name")
            if name_el is not None and name_el.text == "Cleanarr Webhook":
                dest.find("Url").text = webhook_url
                dest.find("NotificationType").text = "ItemMarkPlayed,PlaybackStart,PlaybackStop"
                dest.find("IsEnabled").text = "true"
                dest.find("Template").text = template
                
                header = dest.find("Header")
                if header is None: header = ET.SubElement(dest, "Header")
                header_item = header.find("HeaderItem")
                if header_item is None: header_item = ET.SubElement(header, "HeaderItem")
                
                key_el = header_item.find("Key")
                if key_el is None: key_el = ET.SubElement(header_item, "Key")
                key_el.text = "X-Cleanarr-Webhook-Token"
                
                val_el = header_item.find("Value")
                if val_el is None: val_el = ET.SubElement(header_item, "Value")
                val_el.text = token
                existing = True
                break

        if not existing:
            dest = ET.SubElement(destinations, "GenericDestination")
            ET.SubElement(dest, "Name").text = "Cleanarr Webhook"
            ET.SubElement(dest, "Url").text = webhook_url
            ET.SubElement(dest, "NotificationType").text = "ItemMarkPlayed,PlaybackStart,PlaybackStop"
            ET.SubElement(dest, "IsEnabled").text = "true"
            ET.SubElement(dest, "Template").text = template
            header = ET.SubElement(dest, "Header")
            header_item = ET.SubElement(header, "HeaderItem")
            ET.SubElement(header_item, "Key").text = "X-Cleanarr-Webhook-Token"
            ET.SubElement(header_item, "Value").text = token
            ET.SubElement(dest, "HttpMethod").text = "Post"

        tree = ET.ElementTree(root)
        tree.write(xml_path, encoding="utf-8", xml_declaration=True)
        print(f"Successfully configured Jellyfin webhook XML at {xml_path}")
        
    except Exception as e:
        print(f"Failed to write XML config: {e}")

if __name__ == "__main__":
    configure_jellyfin_webhook()

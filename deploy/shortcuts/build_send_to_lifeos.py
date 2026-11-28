"""Build the "Send to LifeOS" Apple Shortcut as an unsigned .shortcut file.

The shortcut appears in the iPhone share sheet. It asks what LifeOS should do,
converts HEIC photos to smaller JPEGs, and posts everything to /ask/share/
with the person's share key, then shows the server's reply as a notification.

The share address and key are import questions, so the file holds neither:
each person answers them when they add the shortcut.

Sign it on a Mac before sharing (iOS refuses unsigned shortcut files):

    python3 deploy/shortcuts/build_send_to_lifeos.py /tmp/unsigned.shortcut
    shortcuts sign --mode anyone --input /tmp/unsigned.shortcut \\
        --output "deploy/shortcuts/Send to LifeOS.shortcut"
"""

import plistlib
import sys
import uuid

OBJ = "￼"  # Where a variable sits inside a Shortcuts text field.


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def output(uuid_: str, name: str) -> dict:
    return {"Type": "ActionOutput", "OutputUUID": uuid_, "OutputName": name}


def attachment(value: dict) -> dict:
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def text(string: str, **variables: dict) -> dict:
    """A text field; each OBJ in `string` takes the next variable, in order."""
    by_range = {}
    values = list(variables.values())
    position = 0
    for index, char in enumerate(string):
        if char == OBJ:
            by_range[f"{{{index}, 1}}"] = values[position]
            position += 1
    return {
        "Value": {"string": string, "attachmentsByRange": by_range},
        "WFSerializationType": "WFTextTokenString",
    }


def action(identifier: str, **params) -> dict:
    return {"WFWorkflowActionIdentifier": f"is.workflow.actions.{identifier}", "WFWorkflowActionParameters": params}


def build() -> dict:
    address, key, ask, extension, lower = new_id(), new_id(), new_id(), new_id(), new_id()
    resized, jpeg, post = new_id(), new_id(), new_id()
    repeat_group, if_group = new_id(), new_id()
    repeat_item = attachment({"Type": "Variable", "VariableName": "Repeat Item"})

    actions = [
        # 0, 1: answered by the import questions below.
        action("gettext", UUID=address, WFTextActionText="", CustomOutputName="LifeOS address"),
        action("gettext", UUID=key, WFTextActionText="", CustomOutputName="Share key"),
        action("ask", UUID=ask, WFAskActionPrompt="What should LifeOS do with this?", WFInputType="Text"),
        action(
            "repeat.each",
            GroupingIdentifier=repeat_group,
            WFControlFlowMode=0,
            WFInput=attachment({"Type": "ExtensionInput"}),
        ),
        action("properties.files", UUID=extension, WFContentItemPropertyName="File Extension", WFInput=repeat_item),
        action(
            "text.changecase",
            UUID=lower,
            WFCaseType="lowercase",
            text=text(OBJ, ext=output(extension, "File Extension")),
        ),
        action(
            "conditional",
            GroupingIdentifier=if_group,
            WFControlFlowMode=0,
            WFCondition=4,  # "is"
            WFConditionalActionString="heic",
            WFInput={"Type": "Variable", "Variable": attachment(output(lower, "Updated Text"))},
        ),
        # iPhone photos are HEIC, which LifeOS can't read; a 2048 px JPEG
        # stays well under the 5 MB photo limit.
        action("image.resize", UUID=resized, WFImageResizeWidth="2048", WFInput=repeat_item),
        action(
            "image.convert",
            UUID=jpeg,
            WFImageFormat="JPEG",
            WFImageCompressionQuality=0.8,
            WFImagePreserveMetadata=False,
            WFInput=attachment(output(resized, "Resized Image")),
        ),
        action("appendvariable", WFVariableName="Items", WFInput=attachment(output(jpeg, "Converted Image"))),
        action("conditional", GroupingIdentifier=if_group, WFControlFlowMode=1),
        action("appendvariable", WFVariableName="Items", WFInput=repeat_item),
        action("conditional", GroupingIdentifier=if_group, WFControlFlowMode=2, UUID=new_id()),
        action("repeat.each", GroupingIdentifier=repeat_group, WFControlFlowMode=2, UUID=new_id()),
        action(
            "downloadurl",
            UUID=post,
            WFURL=text(OBJ, url=output(address, "Text")),
            WFHTTPMethod="POST",
            ShowHeaders=True,
            WFHTTPHeaders={
                "Value": {"WFDictionaryFieldValueItems": [{
                    "WFItemType": 0,
                    "WFKey": text("Authorization"),
                    "WFValue": text(f"Bearer {OBJ}", key=output(key, "Text")),
                }]},
                "WFSerializationType": "WFDictionaryFieldValue",
            },
            WFHTTPBodyType="Form",
            WFFormValues={
                "Value": {"WFDictionaryFieldValueItems": [
                    {
                        "WFItemType": 0,
                        "WFKey": text("message"),
                        "WFValue": text(OBJ, answer=output(ask, "Provided Input")),
                    },
                    {
                        "WFItemType": 5,  # File
                        "WFKey": text("file"),
                        "WFValue": {
                            "Value": {"Type": "Variable", "VariableName": "Items"},
                            "WFSerializationType": "WFTokenAttachmentParameterState",
                        },
                    },
                ]},
                "WFSerializationType": "WFDictionaryFieldValue",
            },
        ),
        action(
            "notification",
            WFNotificationActionTitle="LifeOS",
            WFNotificationActionBody=text(OBJ, reply=output(post, "Contents of URL")),
            WFNotificationActionSound=True,
        ),
    ]

    return {
        "WFWorkflowClientVersion": "2607.0.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 2071128575, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowInputContentItemClasses": [
            "WFGenericFileContentItem",
            "WFImageContentItem",
            "WFPDFContentItem",
            "WFStringContentItem",
            "WFRichTextContentItem",
        ],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasOutputFallback": False,
        "WFQuickActionSurfaces": [],
        "WFWorkflowImportQuestions": [
            {
                "ActionIndex": 0,
                "Category": "Parameter",
                "ParameterKey": "WFTextActionText",
                "DefaultValue": "",
                "Text": "Paste the share address from LifeOS → Settings → Share from iPhone (it ends in /ask/share/).",
            },
            {
                "ActionIndex": 1,
                "Category": "Parameter",
                "ParameterKey": "WFTextActionText",
                "DefaultValue": "",
                "Text": "Paste your share key from LifeOS → Settings → Share from iPhone. It starts with lifeos_.",
            },
        ],
        "WFWorkflowActions": actions,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: build_send_to_lifeos.py OUTPUT.shortcut")
    with open(sys.argv[1], "wb") as fh:
        plistlib.dump(build(), fh, fmt=plistlib.FMT_BINARY)

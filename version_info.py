# UTF-8
#
# For more information see:
# https://pyinstaller.org/en/stable/usage.html#capturing-windows-version-data
#
VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=(0, 8, 0, 0),
        prodvers=(0, 8, 0, 0),
        mask=0x3F,
        flags=0x0,
        OS=0x40004,          # VOS_NT_WINDOWS32
        fileType=0x1,        # VFT_APP
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",  # US English, Unicode
                    [
                        StringStruct("CompanyName", "LeisureLLM"),
                        StringStruct("FileDescription", "Magic Key Assistant"),
                        StringStruct("FileVersion", "0.8.0"),
                        StringStruct("InternalName", "MagicKeyAssistant"),
                        StringStruct("OriginalFilename", "MagicKeyAssistant.exe"),
                        StringStruct("ProductName", "Magic Key Assistant"),
                        StringStruct("ProductVersion", "0.8.0"),
                        StringStruct(
                            "LegalCopyright",
                            "Copyright (c) 2025-2026 LeisureLLM. All rights reserved.",
                        ),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

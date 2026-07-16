from app.corpus import namespaces
from app.corpus import schema


def test_swift_book_is_language():
    ns = namespaces.namespace_for(
        "https://docs.swift.org/swift-book/documentation/the-swift-programming-language/concurrency",
        "swift-book",
    )
    assert ns == "swift-language"


def test_evolution_is_language():
    assert namespaces.namespace_for(
        "https://github.com/swiftlang/swift-evolution/blob/main/proposals/0296.md",
        "swift-evolution",
    ) == "swift-language"


def test_apple_swift_stdlib():
    assert namespaces.namespace_for(
        "https://developer.apple.com/documentation/swift/array",
        "appledocs-docc",
    ) == "swift-stdlib"


def test_apple_framework():
    assert namespaces.namespace_for(
        "https://developer.apple.com/documentation/swiftui/view",
        "appledocs-docc",
    ) == "apple-framework"


def test_xcode_tooling():
    assert namespaces.namespace_for(
        "https://developer.apple.com/documentation/xcode/build-settings-reference",
        "appledocs-docc",
    ) == "xcode-tooling"


def test_result_is_always_allowed():
    ns = namespaces.namespace_for("https://example.com/whatever", "mystery")
    assert ns in schema.ALLOWED_NAMESPACES

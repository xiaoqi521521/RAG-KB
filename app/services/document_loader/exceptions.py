class DocumentParseError(Exception):
    """Base exception for document loading failures."""


class UnsupportedFileTypeError(DocumentParseError):
    """Raised when no parser supports the requested file type."""


class EmptyDocumentError(DocumentParseError):
    """Raised when parsing produces no usable text."""


class ExternalParserError(DocumentParseError):
    """Raised when an external parser service fails."""

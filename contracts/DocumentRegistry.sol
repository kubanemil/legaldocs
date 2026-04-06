// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract DocumentRegistry {
    struct DocumentAnchor {
        bytes32 contentHash;
        bytes32 resultHash;
        string tenantId;
        uint256 timestamp;
    }

    mapping(string => DocumentAnchor) private anchors;

    event DocumentAnchored(
        string indexed documentId,
        bytes32 contentHash,
        bytes32 resultHash,
        string tenantId,
        uint256 timestamp
    );

    function anchorDocument(
        string calldata documentId,
        bytes32 contentHash,
        bytes32 resultHash,
        string calldata tenantId
    ) external {
        require(anchors[documentId].timestamp == 0, "Document already anchored");

        anchors[documentId] = DocumentAnchor({
            contentHash: contentHash,
            resultHash: resultHash,
            tenantId: tenantId,
            timestamp: block.timestamp
        });

        emit DocumentAnchored(documentId, contentHash, resultHash, tenantId, block.timestamp);
    }

    function getAnchor(string calldata documentId)
        external
        view
        returns (bytes32 contentHash, bytes32 resultHash, string memory tenantId, uint256 timestamp)
    {
        DocumentAnchor storage a = anchors[documentId];
        require(a.timestamp != 0, "Document not found");
        return (a.contentHash, a.resultHash, a.tenantId, a.timestamp);
    }

    function isAnchored(string calldata documentId) external view returns (bool) {
        return anchors[documentId].timestamp != 0;
    }
}

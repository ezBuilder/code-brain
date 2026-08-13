using System;
using UnityEditor;

namespace AIAssetPipeline.Editor
{
    public sealed class AIAssetImportPostprocessor : AssetPostprocessor
    {
        private const string GeneratedSegment = "/Generated/";

        private void OnPreprocessModel()
        {
            if (!assetPath.Contains(GeneratedSegment, StringComparison.Ordinal))
            {
                return;
            }

            var importer = (ModelImporter)assetImporter;
            importer.globalScale = 1f;
            importer.importAnimation = false;
            importer.importBlendShapes = false;
            importer.importCameras = false;
            importer.importLights = false;
            importer.addCollider = false;
            importer.isReadable = false;
            importer.meshCompression = assetPath.Contains("_lod0", StringComparison.OrdinalIgnoreCase)
                ? ModelImporterMeshCompression.Off
                : assetPath.Contains("_lod1", StringComparison.OrdinalIgnoreCase)
                    ? ModelImporterMeshCompression.Low
                    : ModelImporterMeshCompression.Medium;
            importer.optimizeMeshPolygons = true;
            importer.optimizeMeshVertices = true;
            importer.materialImportMode = ModelImporterMaterialImportMode.ImportStandard;
        }
    }
}

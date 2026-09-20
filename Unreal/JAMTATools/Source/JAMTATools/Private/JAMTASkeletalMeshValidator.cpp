#include "JAMTASkeletalMeshValidator.h"

#include "AssetRegistry/AssetData.h"
#include "EditorFramework/AssetImportData.h"
#include "Engine/SkeletalMesh.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Rendering/SkeletalMeshLODRenderData.h"
#include "Rendering/SkeletalMeshRenderData.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"

namespace
{
    struct FJAMExpectedSkeletalMesh
    {
        int32 RenderVertices = 0;
        int32 Triangles = 0;
        int32 MaterialSlots = 0;
        int32 UVChannels = 0;
        int32 DeformBones = 0;
        int32 MaxSkinInfluences = 0;
        int32 SourceErrors = 0;
        int32 SourceWarnings = 0;
    };

    bool FindSidecarForMesh(const USkeletalMesh* Mesh, FString& OutPath)
    {
        if (!Mesh || !Mesh->GetAssetImportData())
        {
            return false;
        }

        const FString Source = Mesh->GetAssetImportData()->GetFirstFilename();
        if (Source.IsEmpty())
        {
            return false;
        }

        OutPath = FPaths::ChangeExtension(Source, TEXT("jammeta.json"));
        return FPaths::FileExists(OutPath);
    }

    // This currently mirrors the static validator. Keeping the readers separate
    // avoids unnecessary abstraction while the validation requirements remain distinct.
    bool ReadExpectedMesh(
        const FString& SidecarPath,
        const FString& MeshName,
        FJAMExpectedSkeletalMesh& OutExpected,
        FString& OutError)
    {
        FString JsonText;
        if (!FFileHelper::LoadFileToString(JsonText, *SidecarPath))
        {
            OutError = FString::Printf(TEXT("Could not read JAM sidecar: %s"), *SidecarPath);
            return false;
        }

        TSharedPtr<FJsonObject> Root;
        const TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(JsonText);
        if (!FJsonSerializer::Deserialize(Reader, Root) || !Root.IsValid())
        {
            OutError = TEXT("JAM sidecar contains invalid JSON.");
            return false;
        }

        FString Schema;
        Root->TryGetStringField(TEXT("schema"), Schema);
        if (Schema != TEXT("jam-ta-tools.export-sidecar.v3"))
        {
            OutError = FString::Printf(TEXT("Unsupported JAM sidecar schema: %s"), *Schema);
            return false;
        }

        const TArray<TSharedPtr<FJsonValue>>* Assets = nullptr;
        if (!Root->TryGetArrayField(TEXT("assets"), Assets) || !Assets)
        {
            OutError = TEXT("JAM sidecar contains no assets array.");
            return false;
        }

        TSharedPtr<FJsonObject> Match;
        for (const TSharedPtr<FJsonValue>& Value : *Assets)
        {
            const TSharedPtr<FJsonObject> Candidate = Value.IsValid() ? Value->AsObject() : nullptr;
            if (!Candidate.IsValid())
            {
                continue;
            }

            FString Name;
            Candidate->TryGetStringField(TEXT("name"), Name);
            if (Name.Equals(MeshName, ESearchCase::IgnoreCase))
            {
                Match = Candidate;
                break;
            }
        }

        // Skeletal exports are often one mesh + one skeleton, so this catches the
        // harmless rename/import cases without making name matching too permissive.
        if (!Match.IsValid() && Assets->Num() == 1)
        {
            Match = (*Assets)[0].IsValid() ? (*Assets)[0]->AsObject() : nullptr;
        }
        if (!Match.IsValid())
        {
            OutError = FString::Printf(TEXT("No sidecar asset record matches '%s'."), *MeshName);
            return false;
        }

        double Number = 0.0;
        if (Match->TryGetNumberField(TEXT("error_count"), Number))
        {
            OutExpected.SourceErrors = static_cast<int32>(Number);
        }
        if (Match->TryGetNumberField(TEXT("warning_count"), Number))
        {
            OutExpected.SourceWarnings = static_cast<int32>(Number);
        }

        const TSharedPtr<FJsonObject>* Metrics = nullptr;
        if (!Match->TryGetObjectField(TEXT("metrics"), Metrics) || !Metrics || !Metrics->IsValid())
        {
            OutError = TEXT("JAM asset record contains no metrics object.");
            return false;
        }

        auto ReadInt = [&Metrics](const TCHAR* Field) -> int32
        {
            double Value = 0.0;
            (*Metrics)->TryGetNumberField(Field, Value);
            return static_cast<int32>(Value);
        };

        OutExpected.RenderVertices = ReadInt(TEXT("render_vertices"));
        OutExpected.Triangles = ReadInt(TEXT("triangles"));
        OutExpected.MaterialSlots = ReadInt(TEXT("material_slots"));
        OutExpected.UVChannels = ReadInt(TEXT("uv_channels"));
        OutExpected.DeformBones = ReadInt(TEXT("deform_bones"));
        OutExpected.MaxSkinInfluences = ReadInt(TEXT("max_skin_influences"));
        return true;
    }
}

bool UJAMTASkeletalMeshValidator::CanValidateAsset_Implementation(
    const FAssetData& InAssetData,
    UObject* InObject,
    FDataValidationContext& InContext) const
{
    const USkeletalMesh* Mesh = Cast<USkeletalMesh>(InObject);
    if (!Mesh)
    {
        return false;
    }

    FString SidecarPath;
    return FindSidecarForMesh(Mesh, SidecarPath);
}

EDataValidationResult UJAMTASkeletalMeshValidator::ValidateLoadedAsset_Implementation(
    const FAssetData& InAssetData,
    UObject* InAsset,
    FDataValidationContext& Context)
{
    USkeletalMesh* Mesh = Cast<USkeletalMesh>(InAsset);
    if (!Mesh)
    {
        return EDataValidationResult::NotValidated;
    }

    FString SidecarPath;
    if (!FindSidecarForMesh(Mesh, SidecarPath))
    {
        return EDataValidationResult::NotValidated;
    }

    FJAMExpectedSkeletalMesh Expected;
    FString ReadError;
    if (!ReadExpectedMesh(SidecarPath, Mesh->GetName(), Expected, ReadError))
    {
        AssetFails(Mesh, FText::FromString(ReadError));
        return EDataValidationResult::Invalid;
    }

    FSkeletalMeshRenderData* RenderData = Mesh->GetResourceForRendering();
    if (!RenderData || RenderData->LODRenderData.Num() == 0)
    {
        AssetFails(Mesh, FText::FromString(TEXT("Unreal has no skeletal render data for LOD0.")));
        return EDataValidationResult::Invalid;
    }

    const FSkeletalMeshLODRenderData& LOD0 = RenderData->LODRenderData[0];
    const int32 ActualVertices = static_cast<int32>(LOD0.GetNumVertices());
    const int32 ActualTriangles = LOD0.GetTotalFaces();
    const int32 ActualUVs = static_cast<int32>(LOD0.GetNumTexCoords());
    const int32 ActualSections = LOD0.RenderSections.Num();
    const int32 ActualMaxInfluences = static_cast<int32>(LOD0.GetVertexBufferMaxBoneInfluences());
    const int32 ActualBones = Mesh->GetRefSkeleton().GetNum();

    bool bFailed = false;
    if (Expected.SourceErrors > 0)
    {
        AssetFails(Mesh, FText::FromString(FString::Printf(
            TEXT("JAM sidecar records %d blocking DCC validation issue(s)."),
            Expected.SourceErrors)));
        bFailed = true;
    }

    if (Expected.Triangles > 0 && Expected.Triangles != ActualTriangles)
    {
        AssetWarning(Mesh, FText::FromString(FString::Printf(
            TEXT("JAM triangle count differs: DCC %d -> Unreal %d."),
            Expected.Triangles, ActualTriangles)));
    }

    if (Expected.RenderVertices > 0)
    {
        const int32 Difference = FMath::Abs(ActualVertices - Expected.RenderVertices);
        const int32 Tolerance = FMath::Max(8, FMath::CeilToInt(Expected.RenderVertices * 0.02f));
        if (Difference > Tolerance)
        {
            AssetWarning(Mesh, FText::FromString(FString::Printf(
                TEXT("JAM render-vertex estimate differs: DCC %d -> Unreal %d (delta %+d)."),
                Expected.RenderVertices, ActualVertices, ActualVertices - Expected.RenderVertices)));
        }
    }

    if (Expected.MaterialSlots > 0 && Expected.MaterialSlots != ActualSections)
    {
        AssetWarning(Mesh, FText::FromString(FString::Printf(
            TEXT("JAM material/section count differs: DCC %d -> Unreal %d."),
            Expected.MaterialSlots, ActualSections)));
    }

    if (Expected.UVChannels > 0 && ActualUVs < Expected.UVChannels)
    {
        AssetWarning(Mesh, FText::FromString(FString::Printf(
            TEXT("JAM expected %d UV channel(s); Unreal LOD0 exposes %d."),
            Expected.UVChannels, ActualUVs)));
    }

    if (Expected.MaxSkinInfluences > 0 && ActualMaxInfluences > Expected.MaxSkinInfluences)
    {
        AssetWarning(Mesh, FText::FromString(FString::Printf(
            TEXT("Skin influence count grew on import: DCC max %d -> Unreal max %d."),
            Expected.MaxSkinInfluences, ActualMaxInfluences)));
    }

    // Reference skeletons can legitimately gain helper/virtual bones later, so this is
    // deliberately one-way. Losing deform bones is the suspicious case here.
    if (Expected.DeformBones > 0 && ActualBones < Expected.DeformBones)
    {
        AssetWarning(Mesh, FText::FromString(FString::Printf(
            TEXT("JAM expected at least %d deform bone(s); Unreal reference skeleton has %d."),
            Expected.DeformBones, ActualBones)));
    }

    if (!bFailed)
    {
        AssetPasses(Mesh);
        return EDataValidationResult::Valid;
    }
    return EDataValidationResult::Invalid;
}

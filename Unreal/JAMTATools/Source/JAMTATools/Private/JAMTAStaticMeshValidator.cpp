#include "JAMTAStaticMeshValidator.h"

#include "AssetRegistry/AssetData.h"
#include "EditorFramework/AssetImportData.h"
#include "Engine/StaticMesh.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "StaticMeshResources.h"

namespace
{
    struct FJAMExpectedMesh
    {
        int32 RenderVertices = 0;
        int32 Triangles = 0;
        int32 MaterialSlots = 0;
        int32 UVChannels = 0;
        int32 SourceErrors = 0;
        int32 SourceWarnings = 0;
        int64 TextureReferenceBytes = 0;
    };

    bool FindSidecarForMesh(const UStaticMesh* Mesh, FString& OutPath)
    {
        if (!Mesh)
        {
            return false;
        }

        const UAssetImportData* ImportData = Mesh->GetAssetImportData();
        if (!ImportData)
        {
            return false;
        }

        const FString Source = ImportData->GetFirstFilename();
        if (Source.IsEmpty())
        {
            return false;
        }

        OutPath = FPaths::ChangeExtension(Source, TEXT("jammeta.json"));
        return FPaths::FileExists(OutPath);
    }

    bool ReadExpectedMesh(const FString& SidecarPath, const FString& MeshName, FJAMExpectedMesh& OutExpected, FString& OutError)
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

        if (!Match.IsValid() && Assets->Num() == 1)
        {
            Match = (*Assets)[0].IsValid() ? (*Assets)[0]->AsObject() : nullptr;
        }
        if (!Match.IsValid())
        {
            OutError = FString::Printf(TEXT("No sidecar asset record matches '%s'."), *MeshName);
            return false;
        }

        OutExpected.SourceErrors = static_cast<int32>(Match->GetNumberField(TEXT("error_count")));
        OutExpected.SourceWarnings = static_cast<int32>(Match->GetNumberField(TEXT("warning_count")));

        const TSharedPtr<FJsonObject>* Metrics = nullptr;
        if (!Match->TryGetObjectField(TEXT("metrics"), Metrics) || !Metrics || !Metrics->IsValid())
        {
            OutError = TEXT("JAM asset record contains no metrics object.");
            return false;
        }

        auto ReadInt = [&Metrics](const TCHAR* Field) -> int32
        {
            double Number = 0.0;
            (*Metrics)->TryGetNumberField(Field, Number);
            return static_cast<int32>(Number);
        };
        auto ReadInt64 = [&Metrics](const TCHAR* Field) -> int64
        {
            double Number = 0.0;
            (*Metrics)->TryGetNumberField(Field, Number);
            return static_cast<int64>(Number);
        };

        OutExpected.RenderVertices = ReadInt(TEXT("render_vertices"));
        OutExpected.Triangles = ReadInt(TEXT("triangles"));
        OutExpected.MaterialSlots = ReadInt(TEXT("material_slots"));
        OutExpected.UVChannels = ReadInt(TEXT("uv_channels"));
        OutExpected.TextureReferenceBytes = ReadInt64(TEXT("texture_reference_bytes"));
        return true;
    }
}

bool UJAMTAStaticMeshValidator::CanValidateAsset_Implementation(
    const FAssetData& InAssetData,
    UObject* InObject,
    FDataValidationContext& InContext) const
{
    const UStaticMesh* Mesh = Cast<UStaticMesh>(InObject);
    if (!Mesh)
    {
        return false;
    }

    FString SidecarPath;
    return FindSidecarForMesh(Mesh, SidecarPath);
}

EDataValidationResult UJAMTAStaticMeshValidator::ValidateLoadedAsset_Implementation(
    const FAssetData& InAssetData,
    UObject* InAsset,
    FDataValidationContext& Context)
{
    UStaticMesh* Mesh = Cast<UStaticMesh>(InAsset);
    if (!Mesh)
    {
        return EDataValidationResult::NotValidated;
    }

    FString SidecarPath;
    if (!FindSidecarForMesh(Mesh, SidecarPath))
    {
        return EDataValidationResult::NotValidated;
    }

    FJAMExpectedMesh Expected;
    FString ReadError;
    if (!ReadExpectedMesh(SidecarPath, Mesh->GetName(), Expected, ReadError))
    {
        AssetFails(Mesh, FText::FromString(ReadError));
        return EDataValidationResult::Invalid;
    }

    // GetLODForExport exposes the render-side data required by the
    // DCC estimator is trying to predict. Source mesh counts are less useful here.
    const FStaticMeshLODResources& LOD0 = Mesh->GetLODForExport(0);
    const int32 ActualVertices = LOD0.GetNumVertices();
    const int32 ActualTriangles = LOD0.GetNumTriangles();
    const int32 ActualUVs = LOD0.GetNumTexCoords();
    const int32 ActualSections = LOD0.Sections.Num();

    bool bFailed = false;
    if (Expected.SourceErrors > 0)
    {
        AssetFails(
            Mesh,
            FText::FromString(FString::Printf(
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

    if (!bFailed)
    {
        AssetPasses(Mesh);
        return EDataValidationResult::Valid;
    }
    return EDataValidationResult::Invalid;
}

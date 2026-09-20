#pragma once

#include "CoreMinimal.h"
#include "EditorValidatorBase.h"
#include "JAMTAStaticMeshValidator.generated.h"

UCLASS()
class JAMTATOOLS_API UJAMTAStaticMeshValidator : public UEditorValidatorBase
{
    GENERATED_BODY()

protected:
    virtual bool CanValidateAsset_Implementation(
        const FAssetData& InAssetData,
        UObject* InObject,
        FDataValidationContext& InContext) const override;

    virtual EDataValidationResult ValidateLoadedAsset_Implementation(
        const FAssetData& InAssetData,
        UObject* InAsset,
        FDataValidationContext& Context) override;
};

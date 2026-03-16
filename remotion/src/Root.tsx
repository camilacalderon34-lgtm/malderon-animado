import React from "react";
import { registerRoot, Composition, CalculateMetadataFunction } from "remotion";
import { TitleCard } from "./TitleCard";
import type { TitleCardProps } from "./TitleCard";
import { ImageScene } from "./ImageScene";
import type { ImageSceneProps } from "./ImageScene";

const calculateMetadata: CalculateMetadataFunction<TitleCardProps> = ({
  props,
}) => {
  return {
    durationInFrames: props.durationInFrames || 150,
    props,
  };
};

const calculateImageSceneMetadata: CalculateMetadataFunction<ImageSceneProps> = ({
  props,
}) => {
  return {
    durationInFrames: props.durationInFrames || 150,
    props,
  };
};

const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="TitleCard"
        component={TitleCard}
        width={1920}
        height={1080}
        fps={30}
        durationInFrames={150}
        defaultProps={{
          titleText: "#10 Title Example",
          durationInFrames: 150,
          backgroundImage: null,
        }}
        calculateMetadata={calculateMetadata}
      />
      <Composition
        id="ImageScene"
        component={ImageScene}
        width={1920}
        height={1080}
        fps={30}
        durationInFrames={150}
        defaultProps={{
          imagePath: "sample.jpg",
          durationInFrames: 150,
          niche: "general",
          orientation: "horizontal" as const,
          imageWidth: 1920,
          imageHeight: 1080,
        }}
        calculateMetadata={calculateImageSceneMetadata}
      />
    </>
  );
};

registerRoot(RemotionRoot);

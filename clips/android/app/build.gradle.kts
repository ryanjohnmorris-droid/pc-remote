plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.clutch.clips"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.clutch.clips"
        minSdk = 29 // Android 10: needed to record game sound
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    // A fixed, throwaway signing key so every build can install over the last one.
    // It is not a secret: anyone can build an app signed with it.
    signingConfigs {
        getByName("debug") {
            storeFile = file("debug.keystore")
            storePassword = "android"
            keyAlias = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
}
